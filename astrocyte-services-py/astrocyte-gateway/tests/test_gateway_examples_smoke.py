"""Smoke-load each ``examples/<name>/astrocyte.yaml`` (CI matrix: ``GATEWAY_EXAMPLE_MATRIX``).

Skipped locally unless the env var is set so ``make ci-gateway-tests`` stays one pytest run
without requiring matrix context.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

pytestmark = pytest.mark.skipif(
    not os.environ.get("GATEWAY_EXAMPLE_MATRIX"),
    reason="Set GATEWAY_EXAMPLE_MATRIX (e.g. tier1-minimal) — used by CI matrix job.",
)


def _reload_app_module() -> None:
    import astrocyte_gateway.app as app_mod

    importlib.reload(app_mod)


def test_matrix_example_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    name = os.environ["GATEWAY_EXAMPLE_MATRIX"].strip()
    cfg = EXAMPLES / name / "astrocyte.yaml"
    assert cfg.is_file(), f"missing {cfg}"

    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")

    if name == "tier1-pgvector":
        if not os.environ.get("DATABASE_URL"):
            pytest.skip("tier1-pgvector example requires DATABASE_URL (pgvector CI job)")

    _reload_app_module()
    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())

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
