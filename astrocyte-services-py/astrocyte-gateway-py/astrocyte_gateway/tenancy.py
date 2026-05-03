"""Gateway-side tenant binding for schema-per-tenant isolation.

This module wires :mod:`astrocyte.tenancy` into the FastAPI app:

1. The configured :class:`~astrocyte.tenancy.TenantExtension` is asked to
   authenticate every inbound request and return a
   :class:`~astrocyte.tenancy.TenantContext`.
2. The tenant's schema name is bound to ``_current_schema`` for the
   lifetime of the request via a Starlette HTTP middleware.
3. Every storage call inside the request handler — recall, retain, wiki,
   observation — automatically routes to that tenant's Postgres schema
   because the adapters resolve their schema via
   :func:`astrocyte.tenancy.fq_table` on every SQL statement.

Default behavior
----------------

If no tenant extension is configured (the common single-tenant case), an
internal :class:`~astrocyte.tenancy.DefaultTenantExtension` is used that
returns the schema name from the ``ASTROCYTE_DATABASE_SCHEMA`` environment
variable, defaulting to ``public``. This means existing deployments need no
configuration changes.

Custom extensions
-----------------

To enable real multi-tenant behavior, configure the gateway with a custom
extension that maps an inbound request to a schema. The extension's
``authenticate(request)`` receives the FastAPI :class:`~fastapi.Request`
directly and is responsible for inspecting headers, JWT claims, etc.

Example custom extension::

    from astrocyte.tenancy import TenantContext, TenantExtension, AuthenticationError

    class HeaderTenantExtension(TenantExtension):
        async def authenticate(self, request) -> TenantContext:
            schema = request.headers.get("X-Tenant-Schema")
            if not schema:
                raise AuthenticationError("missing X-Tenant-Schema header")
            return TenantContext(schema_name=schema)

        async def list_tenants(self):
            # For workers — return whatever set of schemas the deployment serves.
            return [Tenant(schema=s) for s in your_tenant_directory()]

Then pass it to :func:`~astrocyte_gateway.app.create_app` (or the equivalent
factory in your deployment) via ``tenant_extension=HeaderTenantExtension()``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from astrocyte.tenancy import (
    AuthenticationError,
    DEFAULT_SCHEMA,
    DefaultTenantExtension,
    TenantExtension,
    reset_current_schema,
    set_current_schema,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

_logger = logging.getLogger("astrocyte.gateway.tenancy")


def default_tenant_extension() -> TenantExtension:
    """Build the default :class:`TenantExtension` from environment variables.

    Reads ``ASTROCYTE_DATABASE_SCHEMA`` (default ``public``) and returns a
    :class:`DefaultTenantExtension` bound to that schema. This preserves
    single-schema behavior for deployments that don't configure a custom
    extension.
    """
    schema = os.environ.get("ASTROCYTE_DATABASE_SCHEMA", DEFAULT_SCHEMA)
    return DefaultTenantExtension(schema=schema)


# Endpoints that MUST be reachable without tenant binding (k8s liveness etc.).
_TENANCY_BYPASS_PATHS: frozenset[str] = frozenset({"/live", "/health", "/metrics"})


def install_tenant_middleware(app: FastAPI, extension: TenantExtension) -> None:
    """Register HTTP middleware that binds the active tenant schema per request.

    The middleware:

    1. Skips bypass paths (``/live``, ``/health``, ``/metrics``) so liveness
       probes don't require auth.
    2. Calls ``extension.authenticate(request)`` and translates
       :class:`AuthenticationError` into HTTP 401.
    3. Binds the returned schema via :func:`set_current_schema`, runs the
       request handler, and resets the schema in a ``finally`` block so the
       ContextVar never leaks across requests (important if FastAPI ever
       re-uses the same task for back-to-back requests).
    """

    @app.middleware("http")
    async def _tenant_schema_middleware(request: Request, call_next: Any) -> Response:
        if request.url.path in _TENANCY_BYPASS_PATHS:
            return await call_next(request)
        try:
            tenant_ctx = await extension.authenticate(request)
        except AuthenticationError as exc:
            _logger.info("tenant authentication failed for %s: %s", request.url.path, exc)
            return JSONResponse(
                status_code=401,
                content={"error": "authentication_failed", "detail": str(exc)},
            )
        token = set_current_schema(tenant_ctx.schema_name)
        try:
            return await call_next(request)
        finally:
            reset_current_schema(token)


__all__ = ["default_tenant_extension", "install_tenant_middleware"]
