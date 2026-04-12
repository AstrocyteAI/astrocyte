"""HTTP integration against real Postgres + pgvector when DATABASE_URL is set."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set (run in CI gateway-e2e or with local Postgres)",
)


def test_gateway_retain_recall_health_pgvector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tier 1 pgvector against DATABASE_URL.

    Local: set ``bootstrap_schema: true`` (no prior ``migrate.sh``).

    CI (after ``adapters-py/astrocyte-pgvector/scripts/migrate.sh``): set
    ``ASTROCYTE_GATEWAY_E2E_MIGRATED=1`` so the app does not run DDL at runtime.
    """
    migrated = os.environ.get("ASTROCYTE_GATEWAY_E2E_MIGRATED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    cfg = tmp_path / "g.yaml"
    cfg.write_text(
        f"""
provider_tier: storage
vector_store: pgvector
llm_provider: mock
vector_store_config:
  embedding_dimensions: 128
  bootstrap_schema: {str(not migrated).lower()}
barriers:
  pii:
    mode: disabled
escalation:
  degraded_mode: error
access_control:
  enabled: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
    # PgVectorStore reads DATABASE_URL from environment
    from astrocyte_gateway.app import create_app

    app = create_app()
    client = TestClient(app)

    h = client.get("/health")
    assert h.status_code == 200

    bank = "e2e-bank"
    r1 = client.post(
        "/v1/retain",
        json={"content": "integration test memory about planets", "bank_id": bank},
        headers={"X-Astrocyte-Principal": "agent:e2e"},
    )
    assert r1.status_code == 200

    r2 = client.post(
        "/v1/recall",
        json={"query": "planets", "bank_id": bank, "max_results": 5},
        headers={"X-Astrocyte-Principal": "agent:e2e"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert "hits" in body
