"""Optional ASGI app for webhook ingest (requires ``astrocyte[gateway]``).

Uses **Starlette** (pulled in by FastAPI) so the route receives a real
:class:`starlette.requests.Request` (raw body + headers for HMAC). Uvicorn runs this
the same way as a FastAPI app. Full OpenAPI + JWT gateway is roadmap M6.

Use :func:`create_ingest_webhook_app` behind uvicorn or any ASGI server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.applications import Starlette

__all__ = ["create_ingest_webhook_app"]


def create_ingest_webhook_app(
    astrocyte: Any,
    sources: dict[str, Any],
) -> "Starlette":
    """Return a Starlette ASGI app with ``POST /v1/ingest/webhook/{source_id}``.

    ``sources`` maps source id → :class:`~astrocyte.config.SourceConfig` (same as ``astrocyte.yml`` ``sources:``).
    ``astrocyte`` must expose ``async def retain(...)`` like :class:`~astrocyte._astrocyte.Astrocyte`.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from astrocyte.config import SourceConfig
    from astrocyte.ingest.webhook import handle_webhook_ingest

    async def ingest_webhook(request: Request) -> JSONResponse:
        source_id = request.path_params["source_id"]
        cfg = sources.get(source_id)
        if cfg is None:
            return JSONResponse({"ok": False, "error": "unknown source"}, status_code=404)
        if not isinstance(cfg, SourceConfig):
            return JSONResponse({"ok": False, "error": "invalid source config"}, status_code=500)

        raw = await request.body()
        headers = {k: v for k, v in request.headers.items()}

        result = await handle_webhook_ingest(
            source_id=source_id,
            source_config=cfg,
            raw_body=raw,
            headers=headers,
            retain=astrocyte.retain,
        )

        payload: dict[str, Any] = {"ok": result.ok, "error": result.error}
        if result.retain_result is not None:
            rr = result.retain_result
            payload["stored"] = rr.stored
            payload["memory_id"] = rr.memory_id
            payload["deduplicated"] = getattr(rr, "deduplicated", False)
            if rr.error:
                payload["retain_error"] = rr.error
        return JSONResponse(payload, status_code=result.http_status)

    return Starlette(
        routes=[
            Route(
                "/v1/ingest/webhook/{source_id}",
                endpoint=ingest_webhook,
                methods=["POST"],
            ),
        ],
    )
