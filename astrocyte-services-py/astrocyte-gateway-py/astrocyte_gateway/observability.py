"""Request IDs, access logs, and optional OpenTelemetry for the gateway."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_ACCESS = logging.getLogger("astrocyte_gateway.access")


def _json_logs() -> bool:
    return os.environ.get("ASTROCYTE_LOG_FORMAT", "").strip().lower() in ("json", "1", "true", "yes")


def _emit_access(
    *,
    request_id: str,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
) -> None:
    payload: dict[str, Any] = {
        "event": "http_request",
        "request_id": request_id,
        "method": method,
        "path": path,
        "status_code": status_code,
        "duration_ms": round(duration_ms, 3),
    }
    if _json_logs():
        _ACCESS.info(json.dumps(payload, ensure_ascii=False))
    else:
        _ACCESS.info(
            "%s %s %s %s %.2fms",
            request_id,
            method,
            path,
            status_code,
            duration_ms,
        )


class AccessContextMiddleware(BaseHTTPMiddleware):
    """Assign ``X-Request-ID``, echo on response, log one access line per request."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        incoming = (request.headers.get("x-request-id") or "").strip()
        request_id = incoming or str(uuid.uuid4())
        request.state.request_id = request_id

        t0 = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - t0) * 1000.0

        response.headers["X-Request-ID"] = request_id
        _emit_access(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response


def configure_process_logging() -> None:
    """Call before uvicorn starts when ``ASTROCYTE_LOG_FORMAT=json``."""
    if not _json_logs():
        return
    level = getattr(logging, os.environ.get("ASTROCYTE_LOG_LEVEL", "INFO").upper(), logging.INFO)
    for name in ("astrocyte_gateway", "astrocyte_gateway.access"):
        log = logging.getLogger(name)
        log.handlers.clear()
        log.setLevel(level)
        log.propagate = False
        h = logging.StreamHandler()
        h.setFormatter(_JsonLogFormatter())
        log.addHandler(h)
    logging.captureWarnings(True)


class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def maybe_instrument_otel(app: Any) -> None:
    """If ``ASTROCYTE_OTEL_ENABLED`` and ``[otel]`` extra is installed, instrument FastAPI."""
    raw = os.environ.get("ASTROCYTE_OTEL_ENABLED", "").strip().lower()
    if raw not in ("1", "true", "yes"):
        return
    try:
        from astrocyte_gateway.otel_instrument import instrument_app

        instrument_app(app)
    except ImportError:
        logging.getLogger(__name__).warning(
            "ASTROCYTE_OTEL_ENABLED set but OpenTelemetry packages missing; "
            "install: uv sync --extra otel (astrocyte-gateway-py[otel])"
        )
