"""FastAPI application exposing Astrocyte over REST."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
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
    IngestError,
    PiiRejected,
    ProviderUnavailable,
    RateLimited,
)
from astrocyte.ingest.registry import SourceRegistry
from astrocyte.ingest.runtime import retain_callable_for_astrocyte
from astrocyte.ingest.supervisor import IngestSupervisor, merge_source_health
from astrocyte.ingest.webhook import handle_webhook_ingest
from astrocyte.pipeline.mental_model import MentalModelService
from astrocyte.types import AstrocyteContext
from astrocyte.tenancy import TenantExtension
from astrocyte_gateway.auth import get_astrocyte_context
from astrocyte_gateway.brain import build_astrocyte
from astrocyte_gateway.observability import AccessContextMiddleware, maybe_instrument_otel
from astrocyte_gateway.rate_limit import SlidingWindowRateLimitMiddleware, rate_limit_max_from_env
from astrocyte_gateway.serialization import to_jsonable
from astrocyte_gateway.tasks import start_gateway_task_worker
from astrocyte_gateway.tenancy import default_tenant_extension, install_tenant_middleware

# Bounds /health latency when the vector store (e.g. pgvector) cannot connect.
_HEALTH_TIMEOUT_S = 8.0
_logger = logging.getLogger("astrocyte.gateway")


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


async def _warm_reference_stack_providers(brain: Astrocyte) -> None:
    """Warm optional Tier 1 providers so bootstrap DDL runs during startup."""

    pipeline = getattr(brain, "_pipeline", None)
    graph_store = getattr(pipeline, "graph_store", None) if pipeline is not None else None
    for provider in (graph_store, getattr(brain, "_wiki_store", None)):
        await _warm_reference_stack_provider(provider)


async def _warm_reference_stack_provider(provider: object | None) -> None:
    if provider is None:
        return

    # AGE-compatible providers expose schema bootstrap separately from health.
    # Call it directly so startup creates reference-stack tables even with
    # adapter versions whose health check only verifies connectivity.
    ensure_schema = getattr(provider, "_ensure_schema", None)
    if ensure_schema is not None and _callable_without_required_args(ensure_schema):
        await ensure_schema()

    health = getattr(provider, "health", None)
    if health is None:
        return
    status = await health()
    if getattr(status, "healthy", True) is False:
        message = getattr(status, "message", "provider health check failed")
        raise ConfigError(f"Reference stack provider failed startup warm-up: {message}")


def _callable_without_required_args(func: object) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    return all(
        parameter.default is not inspect.Parameter.empty
        or parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }
        for parameter in signature.parameters.values()
    )


def create_app(
    brain: Astrocyte | None = None,
    tenant_extension: TenantExtension | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Args:
        brain: Pre-built ``Astrocyte`` for tests and overhead benchmarks.
        tenant_extension: Custom :class:`~astrocyte.tenancy.TenantExtension`
            for schema-per-tenant deployments. When ``None`` (the default),
            uses :func:`~astrocyte_gateway.tenancy.default_tenant_extension`,
            which reads ``ASTROCYTE_DATABASE_SCHEMA`` (default ``public``)
            and serves a single tenant. Custom extensions can map an inbound
            request to any schema via headers / JWT / API-key lookup.
    """
    if brain is None:
        brain = build_astrocyte()
    if tenant_extension is None:
        tenant_extension = default_tenant_extension()
    ingest_registry = SourceRegistry.from_sources_config(
        brain.config.sources,
        retain=retain_callable_for_astrocyte(brain),
    )
    ingest_supervisor = IngestSupervisor(ingest_registry)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # ``tenant_extension`` is passed through so the worker fans out one
        # pgqueuer poller per tenant. Single-tenant (DefaultTenantExtension)
        # gets exactly one worker — same as before.
        task_worker = await start_gateway_task_worker(brain, tenant_extension)
        await brain.start_background_tasks()
        await _warm_reference_stack_providers(brain)
        await ingest_supervisor.start()
        try:
            yield
        finally:
            await ingest_supervisor.stop()
            await brain.stop_background_tasks()
            if task_worker is not None:
                await task_worker.stop()

    app = FastAPI(
        title="Astrocyte gateway",
        description="HTTP API over astrocyte-py (Tier 1 pipeline; configure with astrocyte.yaml / optional mip.yaml).",
        version="0.1.0",
        lifespan=lifespan,
    )
    _configure_gateway_middleware(app)
    # Bind active tenant schema per request — must be installed AFTER the
    # body-size / CORS / access-context middleware so those run first, but
    # BEFORE any endpoint handler executes. Starlette runs middleware in
    # reverse-registration order (last-added is outermost), which means this
    # call wraps the handler innermost — exactly what we want for the
    # ContextVar lifecycle.
    install_tenant_middleware(app, tenant_extension)

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

    @app.post("/v1/debug/recall")
    async def debug_recall(
        body: dict[str, Any],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Debug-only recall endpoint exposing low-level trace and result counts."""

        result = await recall(body, ctx)
        results = result.get("results", []) if isinstance(result, dict) else []
        return {
            "trace": result.get("trace") if isinstance(result, dict) else None,
            "result_count": len(results) if isinstance(results, list) else None,
        }

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

    def _mental_models() -> MentalModelService:
        wiki_store = getattr(brain, "_wiki_store", None)
        if wiki_store is None:
            raise HTTPException(status_code=501, detail="mental models require a configured wiki_store")
        return MentalModelService(wiki_store)

    @app.post("/v1/mental-models")
    async def create_mental_model(body: dict[str, Any]) -> dict[str, Any]:
        bank_id = body.get("bank_id")
        model_id = body.get("model_id")
        title = body.get("title")
        content = body.get("content")
        if not all(isinstance(value, str) for value in (bank_id, model_id, title, content)):
            raise HTTPException(status_code=400, detail="bank_id, model_id, title, and content are required strings")
        model = await _mental_models().create(
            bank_id=bank_id,
            model_id=model_id,
            title=title,
            content=content,
            scope=str(body.get("scope") or "bank"),
            source_ids=[str(x) for x in body.get("source_ids", [])] if isinstance(body.get("source_ids"), list) else [],
        )
        return to_jsonable(model)

    @app.get("/v1/mental-models")
    async def list_mental_models(bank_id: str, scope: str | None = None) -> dict[str, Any]:
        return {"models": to_jsonable(await _mental_models().list(bank_id, scope=scope))}

    @app.get("/v1/mental-models/{model_id}")
    async def get_mental_model(model_id: str, bank_id: str) -> dict[str, Any]:
        model = await _mental_models().get(bank_id, model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="mental model not found")
        return to_jsonable(model)

    @app.post("/v1/mental-models/{model_id}/refresh")
    async def refresh_mental_model(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        bank_id = body.get("bank_id")
        content = body.get("content")
        if not isinstance(bank_id, str) or not isinstance(content, str):
            raise HTTPException(status_code=400, detail="bank_id and content are required strings")
        model = await _mental_models().refresh(
            bank_id=bank_id,
            model_id=model_id,
            content=content,
            source_ids=[str(x) for x in body.get("source_ids", [])] if isinstance(body.get("source_ids"), list) else None,
        )
        if model is None:
            raise HTTPException(status_code=404, detail="mental model not found")
        return to_jsonable(model)

    @app.delete("/v1/mental-models/{model_id}")
    async def delete_mental_model(model_id: str, bank_id: str) -> dict[str, Any]:
        return {"deleted": await _mental_models().delete(bank_id, model_id)}

    @app.post("/v1/observations/invalidate")
    async def invalidate_observations(body: dict[str, Any]) -> dict[str, Any]:
        bank_id = body.get("bank_id")
        source_ids = body.get("source_ids")
        if not isinstance(bank_id, str) or not isinstance(source_ids, list):
            raise HTTPException(status_code=400, detail="bank_id and source_ids are required")
        pipeline = getattr(brain, "_pipeline", None)
        consolidator = getattr(pipeline, "_observation_consolidator", None)
        vector_store = getattr(pipeline, "vector_store", None)
        if consolidator is None or vector_store is None:
            raise HTTPException(status_code=501, detail="observations require a configured pipeline consolidator")
        deleted = await consolidator.invalidate_sources([str(x) for x in source_ids], bank_id, vector_store)
        return {"deleted": deleted}

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

    @app.post("/v1/dsar/forget_principal")
    async def dsar_forget_principal(
        body: dict[str, Any],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """DSAR right-to-erasure for a single principal across a tenant's banks.

        Called from Cerebro's ``Synapse.DSAR.DeletionWorker`` when an erasure
        request is approved. Sweeps every configured bank whose name starts
        with ``{tenant_id}:`` (Cerebro's multi-tenant naming convention) and
        deletes memories tagged ``principal:{principal}``.

        ## Tag convention

        For a memory to be erasable by this endpoint, it must have been
        retained with the tag ``principal:{principal}`` (e.g.
        ``principal:user:alice``). Callers that want their data covered by
        DSAR must apply this tag at retain time. Memories without the tag
        are NOT deleted — by design — and are reported as ``deleted: 0`` in
        the per-bank breakdown.

        ## Response

            {
              "tenant_id": "...",
              "principal": "user:alice",
              "tag_convention": "principal:user:alice",
              "banks_processed": 3,
              "memories_deleted": 12,
              "details": [
                {"bank_id": "tenant-acme:decisions", "deleted": 7},
                ...
              ]
            }

        ## Compliance bypass

        Calls into ``forget`` with ``compliance=True`` so legal holds are
        bypassed (right-to-erasure overrides retention obligations) AND the
        actor is recorded in the audit log for proof-of-deletion.
        """
        tenant_id = body.get("tenant_id")
        principal = body.get("principal")

        if not isinstance(tenant_id, str) or not tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id (str) is required")
        if not isinstance(principal, str) or not principal:
            raise HTTPException(status_code=400, detail="principal (str) is required")

        configured_banks = brain.config.banks or {}
        tenant_banks = sorted(
            bid for bid in configured_banks.keys() if bid.startswith(f"{tenant_id}:")
        )

        principal_tag = f"principal:{principal}"
        deleted_total = 0
        details: list[dict[str, Any]] = []

        for bank_id in tenant_banks:
            try:
                result = await brain.forget(
                    bank_id,
                    tags=[principal_tag],
                    compliance=True,
                    reason=f"DSAR erasure for {principal}",
                    context=ctx,
                )
                deleted = getattr(result, "deleted_count", 0) or 0
                details.append({"bank_id": bank_id, "deleted": deleted})
                deleted_total += deleted
            except Exception as exc:
                # Don't fail the whole sweep on a single-bank error — record
                # it and continue. Cerebro can re-run the request to retry.
                # Log full detail server-side; surface only a stable
                # error code to the caller.  Raw ``str(exc)`` could leak
                # cross-tenant schema names or internal SQL state (CWE-209).
                _logger.warning(
                    "dsar_forget_principal: bank %s failed: %s",
                    bank_id,
                    exc,
                    exc_info=True,
                )
                details.append({"bank_id": bank_id, "deleted": 0, "error": "internal_error"})

        return {
            "tenant_id": tenant_id,
            "principal": principal,
            "tag_convention": principal_tag,
            "banks_processed": len(tenant_banks),
            "memories_deleted": deleted_total,
            "details": details,
        }

    @app.post("/v1/compile")
    async def compile(
        body: dict[str, Any],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Compile raw memories into wiki pages for a bank (M8).

        Synthesises a structured wiki page for each detected topic scope using
        the LLM. Requires a WikiStore and Tier 1 pipeline to be configured.

        Body:
            bank_id (str, required): Bank to compile.
            scope (str, optional): Compile only memories tagged with this scope.
                                   Omit for full scope discovery (tag grouping +
                                   embedding cluster labelling).
        """
        bank_id = body.get("bank_id")
        if not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="bank_id (str) is required")
        scope = body.get("scope")
        if scope is not None and not isinstance(scope, str):
            raise HTTPException(status_code=400, detail="scope must be a string")
        try:
            result = await brain.compile(bank_id, scope=scope)
        except Exception as exc:
            # Translate ConfigError (no WikiStore / no pipeline) to 422
            if "ConfigError" in type(exc).__name__ or "ProviderUnavailable" in type(exc).__name__:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        return to_jsonable(result)

    @app.post("/v1/graph/search")
    async def graph_search(
        body: dict[str, Any],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Search the knowledge graph for entities matching a name query.

        Returns matching Entity objects. Use the returned entity IDs with
        POST /v1/graph/neighbors to traverse connected memories.

        Body:
            query (str, required): Name or partial name to search.
            bank_id (str, required): Bank whose graph to search.
            limit (int, optional): Max entities to return (default 10).
        """
        query = body.get("query")
        bank_id = body.get("bank_id")
        if not isinstance(query, str) or not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="query and bank_id (str) are required")
        try:
            limit = int(body.get("limit", 10))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="limit must be an integer")
        try:
            entities = await brain.graph_search(query, bank_id, limit=limit)
        except Exception as exc:
            if "ConfigError" in type(exc).__name__:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        return {"entities": to_jsonable(entities)}

    @app.post("/v1/graph/neighbors")
    async def graph_neighbors(
        body: dict[str, Any],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Traverse the knowledge graph from seed entity IDs.

        Walks up to max_depth hops from each seed entity and returns the
        memories connected to discovered entities, scored by proximity.

        Body:
            entity_ids (list[str], required): Seed entity IDs to start from.
            bank_id (str, required): Bank whose graph to traverse.
            max_depth (int, optional): Traversal depth (default 2).
            limit (int, optional): Max memory hits to return (default 20).
        """
        entity_ids = body.get("entity_ids")
        bank_id = body.get("bank_id")
        if not isinstance(entity_ids, list) or not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="entity_ids (list) and bank_id (str) are required")
        try:
            max_depth = int(body.get("max_depth", 2))
            limit = int(body.get("limit", 20))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_depth and limit must be integers")
        try:
            hits = await brain.graph_neighbors(
                [str(e) for e in entity_ids], bank_id, max_depth=max_depth, limit=limit
            )
        except Exception as exc:
            if "ConfigError" in type(exc).__name__:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        return {"hits": to_jsonable(hits)}

    # ── history (M9 time travel) ───────────────────────────────────────────

    @app.post("/v1/history")
    async def history(
        body: dict[str, Any],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Reconstruct what the agent knew at a past point in time (M9).

        Body:
            query (str, required): Recall query to run against the snapshot.
            bank_id (str, required): Bank to query.
            as_of (str, required): ISO 8601 UTC datetime — memories after this
                moment are hidden.
            max_results (int, optional): Default 10.
            max_tokens (int, optional): Token budget for result set.
            tags (list[str], optional): Tag filter.
        """
        query = body.get("query")
        bank_id = body.get("bank_id")
        as_of_raw = body.get("as_of")
        if not isinstance(query, str) or not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="query and bank_id (str) are required")
        if not isinstance(as_of_raw, str):
            raise HTTPException(status_code=400, detail="as_of (ISO 8601 string) is required")
        try:
            as_of = datetime.fromisoformat(as_of_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="as_of must be a valid ISO 8601 datetime string")
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
        result = await brain.history(
            query,
            bank_id,
            as_of,
            max_results=max_results,
            max_tokens=max_tokens,
            tags=[str(t) for t in tags] if isinstance(tags, list) else None,
            context=ctx,
        )
        return to_jsonable(result)

    # ── audit (M10 gap analysis) ───────────────────────────────────────────

    @app.post("/v1/audit")
    async def audit(
        body: dict[str, Any],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Identify knowledge gaps for a topic in a bank (M10).

        Body:
            scope (str, required): Natural-language topic to audit.
            bank_id (str, required): Bank to audit.
            max_memories (int, optional): Memories to scan, default 50.
            max_tokens (int, optional): Token budget for retrieved memories.
            tags (list[str], optional): Tag filter.
        """
        scope = body.get("scope")
        bank_id = body.get("bank_id")
        if not isinstance(scope, str) or not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="scope and bank_id (str) are required")
        try:
            max_memories = int(body["max_memories"]) if body.get("max_memories") is not None else 50
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_memories must be an integer")
        max_tokens = body.get("max_tokens")
        if max_tokens is not None:
            try:
                max_tokens = int(max_tokens)
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="max_tokens must be an integer")
        tags = body.get("tags")
        try:
            result = await brain.audit(
                scope,
                bank_id,
                max_memories=max_memories,
                max_tokens=max_tokens,
                tags=[str(t) for t in tags] if isinstance(tags, list) else None,
            )
        except Exception as exc:
            if "ConfigError" in type(exc).__name__:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        return to_jsonable(result)

    # ── export / import (ops portability) ─────────────────────────────────

    @app.post("/v1/export")
    async def export_bank(
        body: dict[str, Any],
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Export a bank to an AMA JSONL file on the server filesystem (ops).

        Body:
            bank_id (str, required): Bank to export.
            path (str, required): Absolute server-side path to write the JSONL file.
            include_embeddings (bool, optional): Default false.
            include_entities (bool, optional): Default true.
        """
        bank_id = body.get("bank_id")
        path = body.get("path")
        if not isinstance(bank_id, str) or not isinstance(path, str):
            raise HTTPException(status_code=400, detail="bank_id and path (str) are required")
        include_embeddings = bool(body.get("include_embeddings", False))
        include_entities = bool(body.get("include_entities", True))
        try:
            count = await brain.export_bank(
                bank_id,
                path,
                include_embeddings=include_embeddings,
                include_entities=include_entities,
                context=ctx,
            )
        except Exception as exc:
            if "ConfigError" in type(exc).__name__ or "AccessDenied" in type(exc).__name__:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        return {"bank_id": bank_id, "path": path, "exported_count": count}

    @app.post("/v1/import")
    async def import_bank(
        body: dict[str, Any],
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Import memories from an AMA JSONL file on the server filesystem (ops).

        Body:
            bank_id (str, required): Destination bank.
            path (str, required): Absolute server-side path to the JSONL file.
            on_conflict (str, optional): "skip" (default) or "overwrite".
        """
        bank_id = body.get("bank_id")
        path = body.get("path")
        if not isinstance(bank_id, str) or not isinstance(path, str):
            raise HTTPException(status_code=400, detail="bank_id and path (str) are required")
        on_conflict = str(body.get("on_conflict", "skip"))
        if on_conflict not in ("skip", "overwrite"):
            raise HTTPException(status_code=400, detail='on_conflict must be "skip" or "overwrite"')
        try:
            result = await brain.import_bank(
                bank_id,
                path,
                on_conflict=on_conflict,
                context=ctx,
            )
        except Exception as exc:
            if "ConfigError" in type(exc).__name__ or "AccessDenied" in type(exc).__name__:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
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

        # Check whether the running source exposes a custom handle_webhook method
        # (e.g. S3WebhookIngestSource which parses Garage/AWS S3 event notifications).
        source_instance = ingest_supervisor.registry.get(source_id)
        if source_instance is not None and hasattr(source_instance, "handle_webhook"):
            try:
                summary = await source_instance.handle_webhook(raw, headers)
            except IngestError:
                _logger.warning("Custom webhook ingest rejected source_id=%s", source_id, exc_info=True)
                return JSONResponse(
                    content={"ok": False, "error": "webhook ingest rejected"},
                    status_code=400,
                )
            return JSONResponse(content={"ok": True, **summary}, status_code=200)

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

    # ── admin: lifecycle ──────────────────────────────────────────────────

    @app.post("/v1/admin/lifecycle")
    async def admin_lifecycle(
        body: dict[str, Any],
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Run TTL lifecycle sweep on a bank — archives/deletes expired memories.

        Body:
            bank_id (str, required): Bank to sweep.
        """
        _ = ctx
        bank_id = body.get("bank_id")
        if not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="bank_id (str) is required")
        result = await brain.run_lifecycle(bank_id)
        return to_jsonable(result)

    # ── admin: bank health ────────────────────────────────────────────────

    @app.get("/v1/admin/banks/health")
    async def admin_all_bank_health(
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Health scores for all banks that have recorded operations."""
        _ = ctx
        results = await brain.all_bank_health()
        return {"banks": to_jsonable(results)}

    @app.get("/v1/admin/banks/{bank_id}/health")
    async def admin_bank_health(
        bank_id: str,
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Health score and issues for a single bank."""
        _ = ctx
        result = await brain.bank_health(bank_id)
        return to_jsonable(result)

    # ── admin: legal hold ─────────────────────────────────────────────────

    @app.post("/v1/admin/banks/{bank_id}/hold")
    async def admin_set_hold(
        bank_id: str,
        body: dict[str, Any],
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Place a bank under legal hold — blocks forget() until released.

        Body:
            hold_id (str, required): Unique identifier for this hold.
            reason (str, required): Human-readable reason.
            set_by (str, optional): Actor label, default "user:api".
        """
        _ = ctx
        hold_id = body.get("hold_id")
        reason = body.get("reason")
        if not isinstance(hold_id, str) or not isinstance(reason, str):
            raise HTTPException(status_code=400, detail="hold_id and reason (str) are required")
        set_by = str(body.get("set_by", "user:api"))
        hold = brain.set_legal_hold(bank_id, hold_id, reason, set_by=set_by)
        return to_jsonable(hold)

    @app.delete("/v1/admin/banks/{bank_id}/hold/{hold_id}")
    async def admin_release_hold(
        bank_id: str,
        hold_id: str,
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Release a legal hold from a bank. Returns whether the hold existed."""
        _ = ctx
        released = brain.release_legal_hold(bank_id, hold_id)
        return {"bank_id": bank_id, "hold_id": hold_id, "released": released}

    @app.get("/v1/admin/banks/{bank_id}/hold")
    async def admin_check_hold(
        bank_id: str,
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Check whether a bank is currently under legal hold."""
        _ = ctx
        return {"bank_id": bank_id, "under_hold": brain.is_under_hold(bank_id)}

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
