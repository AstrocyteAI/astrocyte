"""Production-oriented middleware: body limit, admin token, optional CORS."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clear_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTROCYTE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ASTROCYTE_MAX_REQUEST_BODY_BYTES", raising=False)
    monkeypatch.delenv("ASTROCYTE_CORS_ORIGINS", raising=False)


def test_admin_routes_require_token_when_configured(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
provider_tier: storage
vector_store: in_memory
llm_provider: mock
barriers: { pii: { mode: disabled } }
escalation: { degraded_mode: error }
access_control: { enabled: false }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
    monkeypatch.setenv("ASTROCYTE_ADMIN_TOKEN", "secret-admin-token")

    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    r = client.get("/v1/admin/banks")
    assert r.status_code == 401

    ok = client.get("/v1/admin/banks", headers={"X-Admin-Token": "secret-admin-token"})
    assert ok.status_code == 200


def test_max_body_rejects_large_content_length(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
provider_tier: storage
vector_store: in_memory
llm_provider: mock
barriers: { pii: { mode: disabled } }
escalation: { degraded_mode: error }
access_control: { enabled: false }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
    monkeypatch.setenv("ASTROCYTE_MAX_REQUEST_BODY_BYTES", "10")

    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    r = client.post(
        "/v1/retain",
        content=b"x" * 100,
        headers={"Content-Type": "application/json", "Content-Length": "100"},
    )
    assert r.status_code == 413


def test_cors_accepts_configured_origin(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
provider_tier: storage
vector_store: in_memory
llm_provider: mock
barriers: { pii: { mode: disabled } }
escalation: { degraded_mode: error }
access_control: { enabled: false }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
    monkeypatch.setenv("ASTROCYTE_CORS_ORIGINS", "https://app.example.com")

    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    pre = client.options(
        "/live",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert pre.status_code == 200
