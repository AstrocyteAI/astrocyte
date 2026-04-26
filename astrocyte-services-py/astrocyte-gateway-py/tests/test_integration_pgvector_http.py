"""HTTP integration against real Postgres + pgvector when DATABASE_URL is set."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set (run in CI gateway-e2e or with local Postgres)",
)


def test_gateway_retain_recall_health_pgvector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Full reference Postgres stack against DATABASE_URL.

    Local: set ``bootstrap_schema: true`` (no prior ``migrate.sh``).

    CI (after ``adapters-storage-py/astrocyte-pgvector/scripts/migrate.sh``): set
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
graph_store: age
wiki_store: pgvector
llm_provider: mock
vector_store_config:
  embedding_dimensions: 128
  bootstrap_schema: {str(not migrated).lower()}
graph_store_config:
  bootstrap_schema: true
wiki_store_config:
  bootstrap_schema: {str(not migrated).lower()}
wiki_compile:
  enabled: true
  auto_start: true
entity_resolution:
  enabled: true
async_tasks:
  enabled: true
  backend: pgqueuer
  install_on_start: true
  auto_start_worker: false
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
    _skip_if_age_unavailable(os.environ["DATABASE_URL"])
    from astrocyte_gateway.app import create_app

    app = create_app()
    with TestClient(app) as client:
        h = client.get("/health")
        assert h.status_code == 200

        bank = "e2e-bank-full-reference"
        r1 = client.post(
            "/v1/retain",
            json={
                "content": "Alice discussed planets yesterday.",
                "bank_id": bank,
                "tags": ["planets"],
                "metadata": {
                    "locomo_persons": "Alice",
                    "temporal_anchor": "2026-02-10",
                    "temporal_phrase": "yesterday",
                    "resolved_date": "2026-02-09",
                    "date_granularity": "day",
                },
            },
            headers={"X-Astrocyte-Principal": "agent:e2e"},
        )
        assert r1.status_code == 200

        compile_response = client.post(
            "/v1/compile",
            json={"bank_id": bank, "scope": "planets"},
            headers={"X-Astrocyte-Principal": "agent:e2e"},
        )
        assert compile_response.status_code == 200
        compile_body = compile_response.json()
        assert compile_body["pages_created"] + compile_body["pages_updated"] >= 1

        r2 = client.post(
            "/v1/recall",
            json={"query": "planets", "bank_id": bank, "max_results": 5},
            headers={"X-Astrocyte-Principal": "agent:e2e"},
        )
        assert r2.status_code == 200
        body = r2.json()
        assert "hits" in body

    _assert_reference_stack_rows(os.environ["DATABASE_URL"], bank)


def _skip_if_age_unavailable(dsn: str) -> None:
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("LOAD 'age'")
    except Exception as exc:
        pytest.skip(f"Apache AGE is not available for full reference-stack integration test: {exc}")


def _assert_reference_stack_rows(dsn: str, bank: str) -> None:
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT 1 FROM astrocyte_banks WHERE id = %s", (bank,)).fetchone() == (1,)
        assert conn.execute(
            "SELECT 1 FROM astrocyte_wiki_pages WHERE bank_id = %s AND scope = 'planets'",
            (bank,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT 1 FROM astrocyte_temporal_facts WHERE bank_id = %s AND temporal_phrase = 'yesterday'",
            (bank,),
        ).fetchone() == (1,)
        assert conn.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_name IN ('astrocyte_entities', 'astrocyte_entity_links', 'astrocyte_memory_entities')
            HAVING count(*) = 3
            """
        ).fetchone() == (1,)
        assert conn.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_name LIKE 'pgqueuer%'
               OR table_name LIKE 'queuer%'
               OR table_name LIKE '%queue%'
            LIMIT 1
            """
        ).fetchone() is not None
