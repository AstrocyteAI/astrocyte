"""Smoke-load each ``examples/<name>/astrocyte.yaml`` (CI matrix: ``GATEWAY_EXAMPLE_MATRIX``).

Skipped locally unless the env var is set so ``make ci-gateway-tests`` stays one pytest run
without requiring matrix context.
"""

from __future__ import annotations

import importlib
import os
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

pytestmark = pytest.mark.skipif(
    not os.environ.get("GATEWAY_EXAMPLE_MATRIX"),
    reason="Set GATEWAY_EXAMPLE_MATRIX (e.g. tier1-minimal) — used by CI matrix job.",
)


def _reload_app_module() -> types.ModuleType:
    mod = importlib.import_module("astrocyte_gateway.app")
    return importlib.reload(mod)


def test_matrix_example_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    name = os.environ["GATEWAY_EXAMPLE_MATRIX"].strip()
    base = EXAMPLES / name / "astrocyte.yaml"
    assert base.is_file(), f"missing {base}"

    if name == "tier1-pgvector":
        if not os.environ.get("DATABASE_URL"):
            pytest.skip("tier1-pgvector example requires DATABASE_URL (pgvector CI job)")
        migrated = os.environ.get("ASTROCYTE_GATEWAY_E2E_MIGRATED", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        # After migrate.sh, schema is owned by SQL migrations — avoid in-app DDL (bootstrap) which
        # can contend with migrated objects and stall. Local dev without MIGRATED still uses the
        # checked-in example (bootstrap_schema: true).
        if migrated:
            cfg = tmp_path / "astrocyte.yaml"
            text = base.read_text(encoding="utf-8").replace(
                "bootstrap_schema: true",
                "bootstrap_schema: false",
            )
            cfg.write_text(text, encoding="utf-8")
        else:
            cfg = base
    else:
        cfg = base

    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")

    app_mod = _reload_app_module()

    with TestClient(app_mod.create_app()) as client:
        live = client.get("/live")
        assert live.status_code == 200

        health = client.get("/health")
        assert health.status_code == 200

        r = client.post(
            "/v1/retain",
            json={"content": "ci matrix smoke", "bank_id": "smoke-bank"},
            headers={"X-Astrocyte-Principal": "user:ci"},
        )
        assert r.status_code == 200

        q = client.post(
            "/v1/recall",
            json={"query": "smoke", "bank_id": "smoke-bank", "max_results": 3},
            headers={"X-Astrocyte-Principal": "user:ci"},
        )
        assert q.status_code == 200
