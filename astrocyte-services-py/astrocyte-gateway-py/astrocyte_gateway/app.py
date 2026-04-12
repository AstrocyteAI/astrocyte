"""FastAPI application exposing Astrocyte over REST."""

from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from astrocyte import Astrocyte
from astrocyte.config import SourceConfig
from astrocyte.errors import (
    AccessDenied,
    CapabilityNotSupported,
    ConfigError,
    PiiRejected,
    ProviderUnavailable,
    RateLimited,
)
from astrocyte.ingest.registry import SourceRegistry
from astrocyte.ingest.runtime import retain_callable_for_astrocyte
from astrocyte.ingest.supervisor import IngestSupervisor, merge_source_health
from astrocyte.ingest.webhook import handle_webhook_ingest
from astrocyte.types import AstrocyteContext

from astrocyte_gateway.auth import get_astrocyte_context
from astrocyte_gateway.brain import build_astrocyte
from astrocyte_gateway.observability import AccessContextMiddleware, maybe_instrument_otel
from astrocyte_gateway.rate_limit import SlidingWindowRateLimitMiddleware, rate_limit_max_from_env
from astrocyte_gateway.serialization import to_jsonable

# Bounds /health latency when the vector store (e.g. pgvector) cannot connect.
_HEALTH_TIMEOUT_S = 8.0


class _MaxBodySizeMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, max_bytes: int) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        cl = request.headers.get("content-length")
        if cl:
            try:
                n = int(cl)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length"},
                )
            if n > self._max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"},
                )
        return await call_next(request)


def _configure_gateway_middleware(app: FastAPI) -> None:
    max_raw = os.environ.get("ASTROCYTE_MAX_REQUEST_BODY_BYTES", "").strip()
    if max_raw:
        try:
            mb = int(max_raw)
            if mb > 0:
                app.add_middleware(_MaxBodySizeMiddleware, max_bytes=mb)
        except ValueError:
            # Invalid env: ignore and leave the default (no explicit body-size cap here).
            pass

    cors = os.environ.get("ASTROCYTE_CORS_ORIGINS", "").strip()
    if cors:
        origins = [o.strip() for o in cors.split(",") if o.strip()]
        if origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_credentials=True,
                allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            )

    # Outermost: request ID + structured access log (see observability.py).
    app.add_middleware(AccessContextMiddleware)

    # Last registered = outermost on the stack — rate limit before other layers (optional).
    rl = rate_limit_max_from_env()
    if rl is not None:
        app.add_middleware(SlidingWindowRateLimitMiddleware, max_per_window=rl)


def require_admin_if_configured(request: Request) -> None:
    """When ``ASTROCYTE_ADMIN_TOKEN`` is set, require matching ``X-Admin-Token`` header."""
    expected = os.environ.get("ASTROCYTE_ADMIN_TOKEN", "").strip()
    if not expected:
        return
    got = (request.headers.get("x-admin-token") or "").strip()
    if not got or len(got) != len(expected) or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token")


def create_app(brain: Astrocyte | None = None) -> FastAPI:
    """Build the FastAPI app. Pass a pre-built ``brain`` for tests and overhead benchmarks."""
    if brain is None:
        brain = build_astrocyte()
    ingest_registry = SourceRegistry.from_sources_config(
        brain.config.sources,
        retain=retain_callable_for_astrocyte(brain),
    )
    ingest_supervisor = IngestSupervisor(ingest_registry)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await ingest_supervisor.start()
        try:
            yield
        finally:
            await ingest_supervisor.stop()

    app = FastAPI(
        title="Astrocyte gateway",
        description="HTTP API over astrocyte-py (Tier 1 pipeline; configure with astrocyte.yaml / optional mip.yaml).",
        version="0.1.0",
        lifespan=lifespan,
    )
    _configure_gateway_middleware(app)

    @app.exception_handler(AccessDenied)
    async def _access_denied(_request: Request, exc: AccessDenied) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(RateLimited)
    async def _rate_limited(_request: Request, exc: RateLimited) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc), "retry_after_seconds": exc.retry_after_seconds},
        )

    @app.exception_handler(ConfigError)
    async def _config_error(_request: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(CapabilityNotSupported)
    async def _capability(_request: Request, exc: CapabilityNotSupported) -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={"detail": str(exc), "provider": exc.provider, "capability": exc.capability},
        )

    @app.exception_handler(PiiRejected)
    async def _pii(_request: Request, exc: PiiRejected) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc), "pii_types": exc.pii_types})

    @app.exception_handler(ProviderUnavailable)
    async def _provider_unavailable(_request: Request, exc: ProviderUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "provider": exc.provider},
        )

    @app.get("/live")
    @app.get("/health/live")
    async def live() -> dict[str, str]:
        """Process is up; does not check PostgreSQL or other dependencies."""
        return {"status": "ok"}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        try:
            status = await asyncio.wait_for(brain.health(), timeout=_HEALTH_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise HTTPException(
                status_code=503,
                detail="Health check timed out (dependencies such as the vector store did not respond in time).",
            ) from e
        return to_jsonable(status)

    @app.get("/health/ingest")
    async def health_ingest() -> dict[str, Any]:
        """Ingest-only readiness: poll/stream/webhook source health (no auth; for ops probes).

        See also ``GET /v1/admin/sources`` when ``ASTROCYTE_ADMIN_TOKEN`` is set for the same data
        behind admin auth.
        """
        rows = await ingest_supervisor.health_snapshot()
        if not rows:
            return {
                "status": "ok",
                "aggregate": {"healthy": True, "message": "no ingest sources configured"},
                "sources": [],
            }
        merged = merge_source_health(rows)
        return {
            "status": "ok" if merged.healthy else "degraded",
            "aggregate": {"healthy": merged.healthy, "message": merged.message},
            "sources": rows,
        }

    @app.post("/v1/retain")
    async def retain(
        body: dict[str, Any],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        content = body.get("content")
        bank_id = body.get("bank_id")
        if not isinstance(content, str) or not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="content and bank_id (str) are required")
        metadata = body.get("metadata")
        tags = body.get("tags")
        result = await brain.retain(
            content,
            bank_id,
            metadata=metadata if isinstance(metadata, dict) else None,
            tags=tags if isinstance(tags, list) else None,
            context=ctx,
        )
        return to_jsonable(result)

    @app.post("/v1/recall")
    async def recall(
        body: dict[str, Any],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        query = body.get("query")
        bank_id = body.get("bank_id")
        banks = body.get("banks")
        if not isinstance(query, str):
            raise HTTPException(status_code=400, detail="query (str) is required")
        try:
            max_results = int(body["max_results"]) if body.get("max_results") is not None else 10
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_results must be an integer")
        max_tokens = body.get("max_tokens")
        if max_tokens is not None:
            try:
                max_tokens = int(max_tokens)
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="max_tokens must be an integer")
        tags = body.get("tags")
        if bank_id is not None and not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="bank_id must be a string")
        if banks is not None and not isinstance(banks, list):
            raise HTTPException(status_code=400, detail="banks must be a list of strings")
        if bank_id is None and banks is None:
            raise HTTPException(status_code=400, detail="bank_id or banks is required")
        result = await brain.recall(
            query,
            bank_id=bank_id if isinstance(bank_id, str) else None,
            banks=[str(x) for x in banks] if isinstance(banks, list) else None,
            max_results=max_results,
            max_tokens=max_tokens,
            tags=[str(x) for x in tags] if isinstance(tags, list) else None,
            context=ctx,
        )
        return to_jsonable(result)

    @app.post("/v1/reflect")
    async def reflect(
        body: dict[str, Any],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        query = body.get("query")
        bank_id = body.get("bank_id")
        if not isinstance(query, str) or not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="query and bank_id (str) are required")
        max_tokens = body.get("max_tokens")
        if max_tokens is not None:
            try:
                max_tokens = int(max_tokens)
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="max_tokens must be an integer")
        include_sources = body.get("include_sources", True)
        result = await brain.reflect(
            query,
            bank_id,
            max_tokens=max_tokens,
            include_sources=bool(include_sources),
            context=ctx,
        )
        return to_jsonable(result)

    @app.post("/v1/forget")
    async def forget(
        body: dict[str, Any],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        bank_id = body.get("bank_id")
        if not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="bank_id (str) is required")
        memory_ids = body.get("memory_ids")
        tags = body.get("tags")
        scope = body.get("scope")
        if scope is not None and scope != "all":
            raise HTTPException(status_code=400, detail='scope must be "all" or omitted')
        result = await brain.forget(
            bank_id,
            memory_ids=[str(x) for x in memory_ids] if isinstance(memory_ids, list) else None,
            tags=[str(x) for x in tags] if isinstance(tags, list) else None,
            scope=scope,
            context=ctx,
        )
        return to_jsonable(result)

    @app.post("/v1/ingest/webhook/{source_id}")
    async def ingest_webhook(
        source_id: str,
        request: Request,
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> Response:
        """Inbound webhook (M4): HMAC or none — see ``sources:`` in config."""
        sources = brain.config.sources or {}
        cfg = sources.get(source_id)
        if cfg is None:
            raise HTTPException(status_code=404, detail="unknown source")
        if not isinstance(cfg, SourceConfig):
            raise HTTPException(status_code=500, detail="invalid source config")

        raw = await request.body()
        headers = {k: v for k, v in request.headers.items()}
        principal: str | None = ctx.principal if ctx is not None else None

        result = await handle_webhook_ingest(
            source_id=source_id,
            source_config=cfg,
            raw_body=raw,
            headers=headers,
            retain=brain.retain,
            principal=principal,
        )

        payload: dict[str, Any] = {"ok": result.ok, "error": result.error}
        if result.retain_result is not None:
            rr = result.retain_result
            payload["stored"] = rr.stored
            payload["memory_id"] = rr.memory_id
            payload["deduplicated"] = getattr(rr, "deduplicated", False)
            if rr.error:
                payload["retain_error"] = rr.error
        return JSONResponse(content=payload, status_code=result.http_status)

    @app.get("/v1/admin/sources")
    async def admin_sources(
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """List configured ingest sources and best-effort health (via :class:`IngestSupervisor`).

        Same per-source rows as **``GET /health/ingest``**, wrapped as ``{"sources": [...]}`` and
        optionally protected by ``ASTROCYTE_ADMIN_TOKEN``.
        """
        _ = ctx
        out = await ingest_supervisor.health_snapshot()
        return {"sources": out}

    @app.get("/v1/admin/banks")
    async def admin_banks(
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Configured bank ids from ``banks:`` (empty if none)."""
        _ = ctx
        banks = brain.config.banks or {}
        return {"banks": list(banks.keys())}

    maybe_instrument_otel(app)
    return app
