"""Webhook ingest route and admin listings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "webhook-ingest" / "astrocyte.yaml"


@pytest.fixture(autouse=True)
def _clear_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")


def test_admin_banks_empty_when_no_banks_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    r = client.get("/v1/admin/banks")
    assert r.status_code == 200
    assert r.json() == {"banks": []}


def test_webhook_unknown_source_404(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    r = client.post("/v1/ingest/webhook/nope", content=b"{}")
    assert r.status_code == 404


def test_webhook_demo_source_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(EXAMPLES))
    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    body = json.dumps({"content": "hello webhook", "content_type": "text"})
    r = client.post(
        "/v1/ingest/webhook/demo",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data.get("stored") is True

    src = client.get("/v1/admin/sources")
    assert src.status_code == 200
    ids = {s["id"] for s in src.json().get("sources", [])}
    assert "demo" in ids
