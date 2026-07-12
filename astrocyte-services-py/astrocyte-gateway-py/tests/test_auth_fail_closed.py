"""Fail-closed guards for unauthenticated public deployments.

Two regressions pinned here:

1. ``create_app`` refuses to build in dev auth mode (no authentication) when
   bound to a non-loopback address, unless ``ASTROCYTE_ALLOW_DEV_AUTH=1``.
2. Admin endpoints return 403 (not open) when ``ASTROCYTE_ADMIN_TOKEN`` is
   unset on a non-loopback deployment.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTROCYTE_CONFIG_PATH", raising=False)
    monkeypatch.delenv("ASTROCYTE_ALLOW_DEV_AUTH", raising=False)
    monkeypatch.delenv("ASTROCYTE_ADMIN_TOKEN", raising=False)


class TestDevModeStartupGuard:
    def test_dev_mode_on_public_host_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        monkeypatch.setenv("ASTROCYTE_HOST", "0.0.0.0")
        from astrocyte_gateway.app import create_app

        with pytest.raises(RuntimeError, match="Refusing to start"):
            create_app()

    def test_dev_mode_on_loopback_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        monkeypatch.setenv("ASTROCYTE_HOST", "127.0.0.1")
        from astrocyte_gateway.app import create_app

        # Should not raise.
        create_app()

    def test_dev_mode_public_with_explicit_optin_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        monkeypatch.setenv("ASTROCYTE_HOST", "0.0.0.0")
        monkeypatch.setenv("ASTROCYTE_ALLOW_DEV_AUTH", "1")
        from astrocyte_gateway.app import create_app

        create_app()

    def test_authenticated_mode_on_public_host_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A configured auth mode is fine on 0.0.0.0 — the guard only fires
        # for dev mode.
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "api_key")
        monkeypatch.setenv("ASTROCYTE_API_KEY", "secret")
        monkeypatch.setenv("ASTROCYTE_HOST", "0.0.0.0")
        from astrocyte_gateway.app import create_app

        create_app()


class TestAdminGuardFailClosed:
    def test_admin_endpoint_denied_when_token_unset_on_public_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        monkeypatch.setenv("ASTROCYTE_HOST", "0.0.0.0")
        monkeypatch.setenv("ASTROCYTE_ALLOW_DEV_AUTH", "1")  # get past startup
        monkeypatch.delenv("ASTROCYTE_ADMIN_TOKEN", raising=False)
        from astrocyte_gateway.app import create_app

        client = TestClient(create_app())
        # Admin lifecycle endpoint must be 403 (fail closed), not open, even
        # though ASTROCYTE_ALLOW_DEV_AUTH lets the app start — the admin guard
        # is a separate decision and defaults closed on a public host. (We set
        # ASTROCYTE_HOST=0.0.0.0 which the request-time guard also reads.)
        r = client.post("/v1/admin/lifecycle", json={})
        assert r.status_code == 403

    def test_admin_endpoint_open_on_loopback_without_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        monkeypatch.setenv("ASTROCYTE_HOST", "127.0.0.1")
        monkeypatch.delenv("ASTROCYTE_ADMIN_TOKEN", raising=False)
        from astrocyte_gateway.app import create_app

        client = TestClient(create_app())
        r = client.post("/v1/admin/lifecycle", json={})
        # Not 403 — loopback keeps the local-dev open behaviour (may be 200 or
        # a downstream error, but the admin guard must not block it).
        assert r.status_code != 403

    def test_admin_endpoint_requires_token_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        monkeypatch.setenv("ASTROCYTE_HOST", "127.0.0.1")
        monkeypatch.setenv("ASTROCYTE_ADMIN_TOKEN", "s3cret-admin")
        from astrocyte_gateway.app import create_app

        client = TestClient(create_app())
        # Wrong / missing token → 401.
        assert client.post("/v1/admin/lifecycle", json={}).status_code == 401
        assert (
            client.post(
                "/v1/admin/lifecycle", json={}, headers={"X-Admin-Token": "wrong"}
            ).status_code
            == 401
        )
