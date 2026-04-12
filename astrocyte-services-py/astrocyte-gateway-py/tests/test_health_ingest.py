"""``GET /health/ingest`` — ingest source snapshot for probes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "webhook-ingest" / "astrocyte.yaml"


@pytest.fixture(autouse=True)
def _clear_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")


def test_health_ingest_no_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    r = client.get("/health/ingest")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["sources"] == []
    assert data["aggregate"]["healthy"] is True
    assert "no ingest" in (data["aggregate"]["message"] or "").lower()


def test_health_ingest_lists_webhook_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(EXAMPLES))
    from astrocyte_gateway.app import create_app

    # Context manager ensures ASGI lifespan (ingest supervisor start) has completed.
    with TestClient(create_app()) as client:
        r = client.get("/health/ingest")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    ids = {s["id"] for s in data.get("sources", [])}
    assert "demo" in ids
    demo_rows = [s for s in data["sources"] if s.get("id") == "demo"]
    assert demo_rows and demo_rows[0].get("healthy") is True
