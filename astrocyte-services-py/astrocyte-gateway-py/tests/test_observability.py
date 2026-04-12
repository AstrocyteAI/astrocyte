"""Request ID + access logging."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_x_request_id_on_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    r = client.get("/live")
    assert r.status_code == 200
    assert "x-request-id" in r.headers
    rid = r.headers["x-request-id"]
    assert len(rid) >= 8

    r2 = client.get("/live", headers={"X-Request-ID": "client-fixed-id"})
    assert r2.headers["x-request-id"] == "client-fixed-id"


def test_json_access_log_emits_json_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
    monkeypatch.setenv("ASTROCYTE_LOG_FORMAT", "json")

    from astrocyte_gateway.observability import configure_process_logging

    configure_process_logging()

    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    client.get("/live")

    err = capsys.readouterr().err
    assert "http_request" in err and "request_id" in err
