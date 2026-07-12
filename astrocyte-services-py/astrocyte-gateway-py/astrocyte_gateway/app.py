"""FastAPI application exposing Astrocyte over REST."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

from astrocyte import Astrocyte
from astrocyte import log_safe as _safe_log
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
from astrocyte.tenancy import TenantExtension
from astrocyte.types import AstrocyteContext
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from astrocyte_gateway.auth import get_astrocyte_context, validate_auth_startup_config
from astrocyte_gateway.brain import build_astrocyte
from astrocyte_gateway.models import (
    AdminLifecycleBody,
    AdminSetHoldBody,
    AuditBody,
    CompileBody,
    DsarForgetPrincipalBody,
    ExportBody,
    ForgetBody,
    GraphNeighborsBody,
    GraphSearchBody,
    HistoryBody,
    ImportBody,
    MentalModelCreateBody,
    MentalModelRefreshBody,
    ObservationsInvalidateBody,
    RecallBody,
    ReflectBody,
    RetainBody,
)
from astrocyte_gateway.observability import AccessContextMiddleware, maybe_instrument_otel
from astrocyte_gateway.rate_limit import SlidingWindowRateLimitMiddleware, rate_limit_max_from_env
from astrocyte_gateway.serialization import to_jsonable
from astrocyte_gateway.tasks import start_gateway_task_worker
from astrocyte_gateway.tenancy import default_tenant_extension, install_tenant_middleware

# Bounds /health latency when the vector store (e.g. pgvector) cannot connect.
_HEALTH_TIMEOUT_S = 8.0
_logger = logging.getLogger("astrocyte.gateway")

# Result/traversal-count clamping (the M1 DoS guard) lives on the request
# models — see astrocyte_gateway.models (``_clamp`` and the per-field
# validators reading ASTROCYTE_MAX_RESULT_LIMIT / ASTROCYTE_GRAPH_MAX_DEPTH).
# Default body-size cap (bytes) applied even when the operator sets nothing —
# bounds per-request memory. Override with ASTROCYTE_MAX_REQUEST_BODY_BYTES
# (``0`` disables the cap entirely).
_DEFAULT_MAX_REQUEST_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB
# Default per-client rate limit (requests/second) applied ONLY when the gateway
# is bound to a non-loopback interface and the operator has not set the env.
# Loopback (local dev, tests, in-process benchmarks) stays unlimited.
_DEFAULT_PUBLIC_RATE_LIMIT_PER_SECOND = 100


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
    # Body-size cap: default-on. Operator value wins; ``0`` disables entirely;
    # invalid/unset falls back to the default cap.
    max_raw = os.environ.get("ASTROCYTE_MAX_REQUEST_BODY_BYTES", "").strip()
    if max_raw:
        try:
            mb = int(max_raw)
        except ValueError:
            mb = _DEFAULT_MAX_REQUEST_BODY_BYTES
    else:
        mb = _DEFAULT_MAX_REQUEST_BODY_BYTES
    if mb > 0:
        app.add_middleware(_MaxBodySizeMiddleware, max_bytes=mb)

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

    # Last registered = outermost on the stack — rate limit before other layers.
    # Explicit env always wins (including ``0``/invalid → disabled). When unset,
    # default-on ONLY for a public (non-loopback) bind — loopback dev, the test
    # suite, and in-process benchmarks stay unlimited.
    rl = _resolve_rate_limit()
    if rl is not None:
        app.add_middleware(SlidingWindowRateLimitMiddleware, max_per_window=rl)


def _resolve_rate_limit() -> int | None:
    """Effective per-second rate limit, or ``None`` to disable the middleware."""
    if os.environ.get("ASTROCYTE_RATE_LIMIT_PER_SECOND", "").strip():
        # Operator spoke — honor it verbatim (0/invalid → disabled).
        return rate_limit_max_from_env()
    host = os.environ.get("ASTROCYTE_HOST", "127.0.0.1").strip()
    if host in _ADMIN_LOOPBACK_HOSTS:
        return None
    return _DEFAULT_PUBLIC_RATE_LIMIT_PER_SECOND


_ADMIN_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def require_admin_if_configured(request: Request) -> None:
    """Guard admin/governance endpoints with ``X-Admin-Token``.

    When ``ASTROCYTE_ADMIN_TOKEN`` is set, require a matching ``X-Admin-Token``
    header. When it is NOT set, fail closed on a public deployment: admin
    endpoints (lifecycle, legal-hold, cross-tenant storage/billing) must never
    be reachable unauthenticated on a non-loopback interface. Only loopback
    (local dev) keeps the open behaviour.

    Note this is deliberately STRICTER than the data-plane dev-auth guard:
    ``ASTROCYTE_ALLOW_DEV_AUTH`` opts a deployment into unauthenticated
    *data* access, but does NOT open the governance/admin surface — to enable
    admin endpoints on a public host you must set an ``ASTROCYTE_ADMIN_TOKEN``.
    """
    expected = os.environ.get("ASTROCYTE_ADMIN_TOKEN", "").strip()
    if not expected:
        host = os.environ.get("ASTROCYTE_HOST", "127.0.0.1").strip()
        if host in _ADMIN_LOOPBACK_HOSTS:
            return
        raise HTTPException(
            status_code=403,
            detail=(
                "Admin endpoints are disabled: ASTROCYTE_ADMIN_TOKEN is not set "
                "and the gateway is bound to a non-loopback address. Set an admin "
                "token to enable these endpoints."
            ),
        )
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
    # Fail closed before wiring anything: refuse an unauthenticated gateway on
    # a public interface (see auth.validate_auth_startup_config for the rules).
    validate_auth_startup_config()

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

    @app.exception_handler(RequestValidationError)
    async def _validation_error_to_400(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Contract: request validation failures are 400 with a readable detail
        # string naming the offending field(s) — the status code and the
        # field-name-in-detail behaviour predate the Pydantic request models
        # (clients and tests match on the field name), so we translate
        # FastAPI's native 422 into the established shape.
        parts = []
        for err in exc.errors()[:5]:
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            parts.append(f"{loc}: {err.get('msg', 'invalid')}" if loc else err.get("msg", "invalid"))
        return JSONResponse(status_code=400, content={"detail": "; ".join(parts) or "invalid request body"})

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

    # ``/health/live`` is a deliberate ALIAS of ``/live`` (same handler), for
    # infra that mounts every probe under a ``/health/*`` prefix. Liveness must
    # check NOTHING beyond "the process responds": its failure means "restart
    # the pod", and restarting the gateway cannot fix a down dependency — a
    # dependency-checking liveness probe turns a DB outage into a restart loop.
    # Dependency readiness lives at ``/health``; ingest-subsystem health at
    # ``/health/ingest``.
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
        body: RetainBody,
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        result = await brain.retain(
            body.content,
            body.bank_id,
            metadata=body.metadata,
            tags=body.tags,
            context=ctx,
        )
        return to_jsonable(result)

    @app.post("/v1/recall")
    async def recall(
        body: RecallBody,
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        result = await brain.recall(
            body.query,
            bank_id=body.bank_id,
            banks=body.banks,
            max_results=body.max_results,
            max_tokens=body.max_tokens,
            tags=body.tags,
            context=ctx,
        )
        return to_jsonable(result)

    @app.post("/v1/debug/recall")
    async def debug_recall(
        body: RecallBody,
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
        body: ReflectBody,
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        result = await brain.reflect(
            body.query,
            body.bank_id,
            max_tokens=body.max_tokens,
            include_sources=body.include_sources,
            context=ctx,
        )
        return to_jsonable(result)

    def _mental_models() -> MentalModelService:
        # M9: prefer the first-class MentalModelStore (set via
        # brain.set_mental_model_store / config.mental_model_store).
        # Falls back to 501 when unconfigured — earlier wiki-piggyback
        # path is gone.
        store = getattr(brain, "_mental_model_store", None)
        if store is None:
            raise HTTPException(
                status_code=501,
                detail=(
                    "mental models require a configured mental_model_store; "
                    "set 'mental_model_store: postgres' (or another registered "
                    "MentalModelStore) in astrocyte.yaml"
                ),
            )
        return MentalModelService(store)

    # Mental-models + observations endpoints take ``ctx`` and route through
    # ``brain.check_access`` for symmetry with /v1/recall, /v1/retain,
    # /v1/reflect, /v1/forget. When ``access_control.enabled = False`` (the
    # default), check_access is a no-op — same effective behaviour as before.
    # When operators turn access control on, these endpoints enforce the same
    # bank-level RBAC as the rest of the API instead of silently bypassing it.

    @app.post("/v1/mental-models")
    async def create_mental_model(
        body: MentalModelCreateBody,
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        brain.check_access(body.bank_id, "write", ctx)
        model = await _mental_models().create(
            bank_id=body.bank_id,
            model_id=body.model_id,
            title=body.title,
            content=body.content,
            scope=body.scope,
            source_ids=body.source_ids,
        )
        return to_jsonable(model)

    @app.get("/v1/mental-models")
    async def list_mental_models(
        bank_id: str,
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
        scope: str | None = None,
    ) -> dict[str, Any]:
        brain.check_access(bank_id, "read", ctx)
        return {"models": to_jsonable(await _mental_models().list(bank_id, scope=scope))}

    @app.get("/v1/mental-models/{model_id}")
    async def get_mental_model(
        model_id: str,
        bank_id: str,
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        brain.check_access(bank_id, "read", ctx)
        model = await _mental_models().get(bank_id, model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="mental model not found")
        return to_jsonable(model)

    @app.post("/v1/mental-models/{model_id}/refresh")
    async def refresh_mental_model(
        model_id: str,
        body: MentalModelRefreshBody,
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        brain.check_access(body.bank_id, "write", ctx)
        model = await _mental_models().refresh(
            bank_id=body.bank_id,
            model_id=model_id,
            content=body.content,
            source_ids=body.source_ids,
        )
        if model is None:
            raise HTTPException(status_code=404, detail="mental model not found")
        return to_jsonable(model)

    @app.delete("/v1/mental-models/{model_id}")
    async def delete_mental_model(
        model_id: str,
        bank_id: str,
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        # Use the ``forget`` permission to mirror /v1/forget — deleting a
        # mental model is destructive and should require the same right as
        # forgetting raw memories.
        brain.check_access(bank_id, "forget", ctx)
        return {"deleted": await _mental_models().delete(bank_id, model_id)}

    @app.post("/v1/observations/invalidate")
    async def invalidate_observations(
        body: ObservationsInvalidateBody,
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        # Invalidating observations cascades into deleting derived rows —
        # same destructive shape as /v1/forget, so guard with ``forget``
        # permission rather than ``write``.
        brain.check_access(body.bank_id, "forget", ctx)
        pipeline = getattr(brain, "_pipeline", None)
        consolidator = getattr(pipeline, "_observation_consolidator", None)
        vector_store = getattr(pipeline, "vector_store", None)
        if consolidator is None or vector_store is None:
            raise HTTPException(status_code=501, detail="observations require a configured pipeline consolidator")
        deleted = await consolidator.invalidate_sources(body.source_ids, body.bank_id, vector_store)
        return {"deleted": deleted}

    @app.post("/v1/forget")
    async def forget(
        body: ForgetBody,
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        result = await brain.forget(
            body.bank_id,
            memory_ids=body.memory_ids,
            tags=body.tags,
            scope=body.scope,
            context=ctx,
        )
        return to_jsonable(result)

    @app.post("/v1/dsar/forget_principal")
    async def dsar_forget_principal(
        body: DsarForgetPrincipalBody,
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
        tenant_id = body.tenant_id
        principal = body.principal

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
        body: CompileBody,
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
        try:
            result = await brain.compile(body.bank_id, scope=body.scope, context=ctx)
        except Exception as exc:
            # Translate ConfigError (no WikiStore / no pipeline) to 422
            if "ConfigError" in type(exc).__name__ or "ProviderUnavailable" in type(exc).__name__:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        return to_jsonable(result)

    @app.post("/v1/graph/search")
    async def graph_search(
        body: GraphSearchBody,
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
        try:
            entities = await brain.graph_search(body.query, body.bank_id, limit=body.limit, context=ctx)
        except Exception as exc:
            if "ConfigError" in type(exc).__name__:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        return {"entities": to_jsonable(entities)}

    @app.post("/v1/graph/neighbors")
    async def graph_neighbors(
        body: GraphNeighborsBody,
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
        try:
            hits = await brain.graph_neighbors(
                body.entity_ids, body.bank_id, max_depth=body.max_depth, limit=body.limit, context=ctx
            )
        except Exception as exc:
            if "ConfigError" in type(exc).__name__:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        return {"hits": to_jsonable(hits)}

    # ── history (M9 time travel) ───────────────────────────────────────────

    @app.post("/v1/history")
    async def history(
        body: HistoryBody,
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
        result = await brain.history(
            body.query,
            body.bank_id,
            body.as_of,
            max_results=body.max_results,
            max_tokens=body.max_tokens,
            tags=body.tags,
            context=ctx,
        )
        return to_jsonable(result)

    # ── audit (M10 gap analysis) ───────────────────────────────────────────

    @app.post("/v1/audit")
    async def audit(
        body: AuditBody,
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
        try:
            result = await brain.audit(
                body.scope,
                body.bank_id,
                max_memories=body.max_memories,
                max_tokens=body.max_tokens,
                tags=body.tags,
                context=ctx,
            )
        except Exception as exc:
            if "ConfigError" in type(exc).__name__:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        return to_jsonable(result)

    # ── export / import (ops portability) ─────────────────────────────────

    @app.post("/v1/export")
    async def export_bank(
        body: ExportBody,
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
        bank_id, path = body.bank_id, body.path
        try:
            count = await brain.export_bank(
                bank_id,
                path,
                include_embeddings=body.include_embeddings,
                include_entities=body.include_entities,
                context=ctx,
            )
        except ValueError as exc:
            # Path containment failures (CWE-022) and AMA validation errors
            # surface as ValueError from astrocyte.portability — return 422
            # so operators see "set ASTROCYTE_PORTABILITY_ROOTS" hint.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            if "ConfigError" in type(exc).__name__ or "AccessDenied" in type(exc).__name__:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        return {"bank_id": bank_id, "path": path, "exported_count": count}

    @app.post("/v1/import")
    async def import_bank(
        body: ImportBody,
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Import memories from an AMA JSONL file on the server filesystem (ops).

        Body:
            bank_id (str, required): Destination bank.
            path (str, required): Absolute server-side path to the JSONL file.
            on_conflict (str, optional): "skip" (default) or "overwrite".
        """
        try:
            result = await brain.import_bank(
                body.bank_id,
                body.path,
                on_conflict=body.on_conflict,
                context=ctx,
            )
        except ValueError as exc:
            # Path containment failures (CWE-022) and AMA validation errors
            # surface as ValueError from astrocyte.portability — return 422
            # so operators see "set ASTROCYTE_PORTABILITY_ROOTS" hint.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
                _logger.warning("Custom webhook ingest rejected source_id=%s", _safe_log(source_id), exc_info=True)
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
        body: AdminLifecycleBody,
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> dict[str, Any]:
        """Run TTL lifecycle sweep on a bank — archives/deletes expired memories.

        Body:
            bank_id (str, required): Bank to sweep.
        """
        _ = ctx
        result = await brain.run_lifecycle(body.bank_id)
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
        body: AdminSetHoldBody,
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
        hold = brain.set_legal_hold(bank_id, body.hold_id, body.reason, set_by=body.set_by)
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

    # ── admin: per-tenant storage billing ────────────────────────────────
    #
    # Cerebro's StoragePollWorker polls this endpoint hourly for every
    # tenant's ``bytes_used`` figure and feeds the result into Stripe
    # metered billing. See ``docs/_design/storage-billing-endpoint.md`` for
    # the full contract; this endpoint is the canonical handshake (§10.3).
    #
    # The endpoint reads from ``public.astrocyte_tenant_storage_snapshots``,
    # which is populated by the snapshot worker (see
    # ``astrocyte_gateway.storage_snapshot``). Endpoint latency is decoupled
    # from measurement cost: the read is a single PK lookup against a small
    # cross-tenant table, regardless of how much data the tenant has.

    @app.get("/v1/admin/tenants/{tenant_id}/storage")
    async def admin_tenant_storage(
        tenant_id: str,
        _admin: Annotated[None, Depends(require_admin_if_configured)],
        ctx: Annotated[AstrocyteContext | None, Depends(get_astrocyte_context)],
    ) -> Response:
        """Per-tenant storage figure for billing (design contract §10.3).

        Reads the most recent snapshot row from
        ``public.astrocyte_tenant_storage_snapshots`` and returns the
        ``bytes_used`` figure plus a freshness indicator
        (``snapshot_age_seconds``) so callers can reason about staleness
        without HTTP cache headers.

        Path params:
            tenant_id: Opaque external tenant identifier (Cerebro tenant id).
                Echoed back in the response so callers can detect mis-routed
                replies.

        Responses:
            200: full design-§10.3 body.
            401: ``X-Admin-Token`` missing or wrong (when configured).
            404: no snapshot row for ``tenant_id``. Treat as a brand-new
                Free tenant that hasn't written anything yet — Cerebro
                caches ``bytes_used = 0`` for this case.

        Database access: opens a one-shot psycopg connection per request
        using ``DATABASE_URL``. The read is a single PK lookup so the
        per-request cost is dominated by connection establishment. A
        future slice introduces a pool if endpoint RPS warrants it
        (design §6.2 capacity model — at 10k tenants we switch Cerebro
        to the bulk endpoint and per-tenant RPS drops to ad-hoc).
        """
        _ = ctx
        from astrocyte_gateway.storage_snapshot import fetch_snapshot

        dsn = os.environ.get("DATABASE_URL", "").strip()
        if not dsn:
            # No DATABASE_URL configured — the snapshot table cannot be
            # read at all. This is structurally distinct from "no row for
            # this tenant" (which is 404), so surface it as 503.
            return JSONResponse(
                status_code=503,
                content={
                    "error": "storage_snapshot_unavailable",
                    "message": "DATABASE_URL not configured on this gateway",
                },
            )

        import psycopg

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            snapshot = await fetch_snapshot(conn, tenant_id)

        if snapshot is None:
            # No row yet — either a brand-new tenant that has never been
            # measured, or one whose schema doesn't exist. Both map to 404
            # per design §3.5; Cerebro treats this as bytes_used=0 and
            # does not alarm.
            return JSONResponse(
                status_code=404,
                content={
                    "error": "tenant_not_found",
                    "message": "no snapshot row yet for tenant_id",
                },
            )

        # Compute snapshot_age_seconds at response time so callers see the
        # freshness as of NOW, not as of when the worker last ran. The
        # measured_at column is timezone-aware (TIMESTAMPTZ); compare in UTC.
        now = datetime.now(timezone.utc)
        measured_at_dt = snapshot.measured_at  # type: ignore[assignment]
        snapshot_age = max(0, int((now - measured_at_dt).total_seconds()))  # type: ignore[operator]

        # last_write_at and memory_count are optional in the contract
        # (§3.1). Serialise to None when the worker hasn't populated them
        # yet — Cerebro must tolerate either presence (§10.6 says the JSON
        # shape is open).
        last_write_at = snapshot.last_write_at
        breakdown: dict[str, Any] = {
            "heap_bytes": snapshot.heap_bytes,
            "index_bytes": snapshot.index_bytes,
            "table_count": snapshot.table_count,
            "memory_count": snapshot.memory_count,
            "last_write_at": _iso8601_z(last_write_at) if last_write_at is not None else None,
        }
        if snapshot.measure_error is not None:
            # Surface the failure to Cerebro as an optional warning. The
            # row's bytes_used is the LAST-KNOWN-good value (the worker
            # writes it as 0 on failure), so the caller still gets a 200
            # — they can choose to log the warning but not block billing.
            breakdown["warnings"] = [snapshot.measure_error]

        return JSONResponse(
            status_code=200,
            content={
                "tenant_id": snapshot.tenant_id,
                "schema": snapshot.schema_name,
                "bytes_used": snapshot.bytes_used,
                "measured_at": _iso8601_z(measured_at_dt),
                "snapshot_age_seconds": snapshot_age,
                "breakdown": breakdown,
            },
        )

    maybe_instrument_otel(app)
    return app


def _iso8601_z(value: Any) -> str:
    """Format a tz-aware datetime as ISO 8601 in UTC with a trailing ``Z``.

    The design contract (§10.6) pins the wire format so downstream
    consumers don't have to handle ``+00:00`` vs ``Z`` vs offset variants.
    Naive datetimes are treated as UTC — this should never happen since
    the snapshot table column is ``TIMESTAMPTZ``, but we defend against
    it explicitly so a future migration that drops the tz suffix does not
    silently break the wire format.
    """
    from datetime import timezone as _tz

    if value.tzinfo is None:
        value = value.replace(tzinfo=_tz.utc)
    else:
        value = value.astimezone(_tz.utc)
    # ``isoformat()`` for a UTC datetime ends in ``+00:00``; swap to ``Z``
    # so the wire shape matches the contract verbatim.
    iso = value.isoformat()
    if iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"
    return iso
