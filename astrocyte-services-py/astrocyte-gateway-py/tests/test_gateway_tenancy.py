"""Tests for the gateway tenant-binding middleware."""

from __future__ import annotations

import pytest
from astrocyte.tenancy import (
    AuthenticationError,
    DEFAULT_SCHEMA,
    DefaultTenantExtension,
    Tenant,
    TenantContext,
    TenantExtension,
    get_current_schema,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app_with_probe(extension: TenantExtension) -> FastAPI:
    """Build a minimal FastAPI app with the tenant middleware + a probe endpoint.

    The probe endpoint returns the active schema name from inside the request
    handler — exactly the surface the storage adapters see.
    """
    from astrocyte_gateway.tenancy import install_tenant_middleware

    app = FastAPI()
    install_tenant_middleware(app, extension)

    @app.get("/probe")
    async def probe():
        return {"schema": get_current_schema()}

    @app.get("/health")
    async def health():
        # Bypass path — must NOT require auth.
        return {"status": "ok", "schema_at_handler": get_current_schema()}

    return app


class TestDefaultExtension:
    """Default behavior — single-schema deployments need no configuration."""

    def test_default_extension_binds_public(self):
        app = _make_app_with_probe(DefaultTenantExtension())
        client = TestClient(app)
        resp = client.get("/probe")
        assert resp.status_code == 200
        assert resp.json() == {"schema": "public"}
        assert DEFAULT_SCHEMA == "public"

    def test_explicit_default_schema(self):
        app = _make_app_with_probe(DefaultTenantExtension(schema="tenant_acme"))
        client = TestClient(app)
        resp = client.get("/probe")
        assert resp.json() == {"schema": "tenant_acme"}


class TestCustomExtension:
    """A real multi-tenant extension reads request data to pick a schema."""

    def test_header_based_extension_binds_per_request(self):
        class HeaderExtension(TenantExtension):
            async def authenticate(self, request) -> TenantContext:
                schema = request.headers.get("X-Tenant-Schema")
                if not schema:
                    raise AuthenticationError("missing X-Tenant-Schema")
                return TenantContext(schema_name=schema)

            async def list_tenants(self):
                return []

        app = _make_app_with_probe(HeaderExtension())
        client = TestClient(app)

        r1 = client.get("/probe", headers={"X-Tenant-Schema": "tenant_acme"})
        assert r1.json() == {"schema": "tenant_acme"}

        r2 = client.get("/probe", headers={"X-Tenant-Schema": "tenant_globex"})
        assert r2.json() == {"schema": "tenant_globex"}

    def test_authentication_error_returns_401(self):
        class StrictExtension(TenantExtension):
            async def authenticate(self, request) -> TenantContext:
                raise AuthenticationError("no api key supplied")

            async def list_tenants(self):
                return []

        app = _make_app_with_probe(StrictExtension())
        client = TestClient(app)
        resp = client.get("/probe")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"] == "authentication_failed"
        assert "no api key supplied" in body["detail"]

    def test_request_object_passed_to_authenticate(self):
        """The TenantExtension contract takes ``Any`` for context — the gateway
        passes the FastAPI :class:`~fastapi.Request` so extensions can inspect
        headers, query params, body, etc."""
        captured: dict[str, str] = {}

        class CapturingExtension(TenantExtension):
            async def authenticate(self, request) -> TenantContext:
                captured["path"] = str(request.url.path)
                captured["method"] = request.method
                captured["ua"] = request.headers.get("user-agent", "")
                return TenantContext(schema_name="probe_schema")

            async def list_tenants(self):
                return [Tenant(schema="probe_schema")]

        app = _make_app_with_probe(CapturingExtension())
        client = TestClient(app)
        client.get("/probe", headers={"User-Agent": "test-suite/1.0"})

        assert captured["path"] == "/probe"
        assert captured["method"] == "GET"
        assert captured["ua"] == "test-suite/1.0"


class TestBypassPaths:
    """``/health``, ``/live``, ``/metrics`` MUST be reachable without auth.

    These are k8s liveness/readiness probes — failing them on tenant-auth
    error would take the pod offline, not what anyone wants.
    """

    def test_health_skips_authentication(self):
        class AlwaysFailExtension(TenantExtension):
            async def authenticate(self, request) -> TenantContext:
                raise AuthenticationError("denied")

            async def list_tenants(self):
                return []

        app = _make_app_with_probe(AlwaysFailExtension())
        client = TestClient(app)

        # /probe is gated → 401
        assert client.get("/probe").status_code == 401

        # /health is bypassed → 200
        resp = client.get("/health")
        assert resp.status_code == 200
        # The handler runs without a bound schema, so it falls back to default.
        assert resp.json()["status"] == "ok"


class TestSchemaResetAcrossRequests:
    """The ContextVar must reset between requests so requests don't leak state.

    Even though Python's ContextVar is per-async-task (and FastAPI typically
    runs each request in a fresh task), we explicitly reset in a ``finally``
    block as defense-in-depth.
    """

    def test_schema_reset_after_each_request(self):
        seen: list[str] = []

        class AlternatingExtension(TenantExtension):
            def __init__(self):
                self._next = "tenant_acme"

            async def authenticate(self, request) -> TenantContext:
                schema = self._next
                self._next = "tenant_globex" if schema == "tenant_acme" else "tenant_acme"
                return TenantContext(schema_name=schema)

            async def list_tenants(self):
                return []

        app = _make_app_with_probe(AlternatingExtension())
        client = TestClient(app)

        for _ in range(4):
            resp = client.get("/probe")
            seen.append(resp.json()["schema"])

        assert seen == ["tenant_acme", "tenant_globex", "tenant_acme", "tenant_globex"]


class TestEnvironmentDefault:
    """Without explicit configuration, ``ASTROCYTE_DATABASE_SCHEMA`` env var
    determines the default extension's schema."""

    def test_env_var_drives_default(self, monkeypatch: pytest.MonkeyPatch):
        from astrocyte_gateway.tenancy import default_tenant_extension

        monkeypatch.setenv("ASTROCYTE_DATABASE_SCHEMA", "tenant_from_env")
        ext = default_tenant_extension()
        app = _make_app_with_probe(ext)
        resp = TestClient(app).get("/probe")
        assert resp.json() == {"schema": "tenant_from_env"}

    def test_env_default_is_public(self, monkeypatch: pytest.MonkeyPatch):
        from astrocyte_gateway.tenancy import default_tenant_extension

        monkeypatch.delenv("ASTROCYTE_DATABASE_SCHEMA", raising=False)
        ext = default_tenant_extension()
        app = _make_app_with_probe(ext)
        resp = TestClient(app).get("/probe")
        assert resp.json() == {"schema": "public"}
