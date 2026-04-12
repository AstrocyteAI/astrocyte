"""Optional per-client sliding-window rate limit (HTTP gateway edge hardening)."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Any

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Avoid unbounded memory if many spoofed X-Forwarded-For values hit the gateway.
_MAX_TRACKED_CLIENTS = 5000
_WINDOW_S = 1.0


def _client_key(request: Request) -> str:
    raw = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if raw:
        return raw[:128]
    if request.client:
        return request.client.host[:128]
    return "unknown"


def _exempt_path(path: str) -> bool:
    """Liveness/readiness probes should not consume the API quota."""
    if path in ("/live", "/health/live"):
        return True
    if path == "/health" or path.startswith("/health/"):
        return True
    return False


class SlidingWindowRateLimitMiddleware(BaseHTTPMiddleware):
    """Reject excess requests with **429** when a client exceeds *max_per_window* per **1 s** rolling window."""

    def __init__(self, app: Any, max_per_window: int) -> None:
        super().__init__(app)
        self._max = max(1, int(max_per_window))
        self._lock = asyncio.Lock()
        self._hits: dict[str, deque[float]] = {}

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if _exempt_path(request.url.path):
            return await call_next(request)

        key = _client_key(request)
        now = time.monotonic()

        async with self._lock:
            if len(self._hits) >= _MAX_TRACKED_CLIENTS and key not in self._hits:
                # Fail closed on pathological client fan-out; operators should terminate TLS at an edge with real IPs.
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit table full; configure edge proxy or raise limits"},
                    headers={"Retry-After": "1"},
                )

            dq = self._hits.setdefault(key, deque())
            while dq and dq[0] < now - _WINDOW_S:
                dq.popleft()
            if len(dq) >= self._max:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded"},
                    headers={"Retry-After": "1"},
                )
            dq.append(now)

        return await call_next(request)


def rate_limit_max_from_env() -> int | None:
    """Parse ``ASTROCYTE_RATE_LIMIT_PER_SECOND`` — positive int = enabled; missing/invalid = disabled."""
    raw = os.environ.get("ASTROCYTE_RATE_LIMIT_PER_SECOND", "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    if n <= 0:
        return None
    return n
