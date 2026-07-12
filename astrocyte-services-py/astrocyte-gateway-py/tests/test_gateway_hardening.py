"""Production-oriented middleware: body limit, admin token, optional CORS."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clear_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTROCYTE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ASTROCYTE_MAX_REQUEST_BODY_BYTES", raising=False)
    monkeypatch.delenv("ASTROCYTE_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("ASTROCYTE_RATE_LIMIT_PER_SECOND", raising=False)


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


def test_rate_limit_returns_429_when_exceeded(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
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
    monkeypatch.setenv("ASTROCYTE_RATE_LIMIT_PER_SECOND", "1")

    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    assert client.get("/live").status_code == 200
    assert client.post(
        "/v1/recall",
        json={"query": "r", "bank_id": "b1", "max_results": 1},
    ).status_code == 200
    limited = client.post(
        "/v1/recall",
        json={"query": "r", "bank_id": "b1", "max_results": 1},
    )
    assert limited.status_code == 429
    assert limited.json().get("detail")
    assert limited.headers.get("retry-after") == "1"


# ── M1 DoS: result-limit clamps + default-on body/rate ──────────────────────


def test_clamp_bounds_into_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """The M1 DoS clamp now lives on the request models (models._clamp)."""
    from astrocyte_gateway.models import _clamp

    monkeypatch.delenv("ASTROCYTE_MAX_RESULT_LIMIT", raising=False)
    kw = {"minimum": 1, "env_var": "ASTROCYTE_MAX_RESULT_LIMIT", "default_ceiling": 1000}
    # In range → passthrough.
    assert _clamp(50, **kw) == 50
    # Over ceiling → clamped down (the DoS case).
    assert _clamp(1_000_000_000, **kw) == 1000
    # Below minimum → clamped up.
    assert _clamp(-5, **kw) == 1
    # Env ceiling wins over the default.
    monkeypatch.setenv("ASTROCYTE_MAX_RESULT_LIMIT", "20")
    assert _clamp(50, **kw) == 20


def test_recall_model_rejects_non_integer_max_results() -> None:
    """Non-integer max_results is a validation error (HTTP layer maps it to 400)."""
    import pydantic

    from astrocyte_gateway.models import RecallBody

    with pytest.raises(pydantic.ValidationError, match="max_results"):
        RecallBody(query="q", bank_id="b", max_results="not-an-int")


def test_recall_clamps_oversized_max_results_instead_of_rejecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
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

    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    # A pathological max_results must succeed (clamped), not 400 or OOM.
    r = client.post(
        "/v1/recall",
        json={"query": "r", "bank_id": "b1", "max_results": 1_000_000_000},
    )
    assert r.status_code == 200


def test_body_size_cap_is_default_on_and_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    from astrocyte_gateway.app import (
        _configure_gateway_middleware,
        _MaxBodySizeMiddleware,
    )

    # Unset → body-size middleware present by default.
    monkeypatch.delenv("ASTROCYTE_MAX_REQUEST_BODY_BYTES", raising=False)
    app = FastAPI()
    _configure_gateway_middleware(app)
    assert any(m.cls is _MaxBodySizeMiddleware for m in app.user_middleware)

    # Explicit 0 → disabled.
    monkeypatch.setenv("ASTROCYTE_MAX_REQUEST_BODY_BYTES", "0")
    app2 = FastAPI()
    _configure_gateway_middleware(app2)
    assert not any(m.cls is _MaxBodySizeMiddleware for m in app2.user_middleware)


def test_rate_limit_default_on_public_off_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    from astrocyte_gateway.app import (
        _DEFAULT_PUBLIC_RATE_LIMIT_PER_SECOND,
        _resolve_rate_limit,
    )

    monkeypatch.delenv("ASTROCYTE_RATE_LIMIT_PER_SECOND", raising=False)

    # Loopback (dev/tests) → unlimited.
    monkeypatch.setenv("ASTROCYTE_HOST", "127.0.0.1")
    assert _resolve_rate_limit() is None

    # Public bind → default-on.
    monkeypatch.setenv("ASTROCYTE_HOST", "0.0.0.0")
    assert _resolve_rate_limit() == _DEFAULT_PUBLIC_RATE_LIMIT_PER_SECOND

    # Explicit env wins on a public bind — including 0 to disable.
    monkeypatch.setenv("ASTROCYTE_RATE_LIMIT_PER_SECOND", "0")
    assert _resolve_rate_limit() is None
    monkeypatch.setenv("ASTROCYTE_RATE_LIMIT_PER_SECOND", "25")
    assert _resolve_rate_limit() == 25
