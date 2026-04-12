"""``create_app(brain=...)`` allows benchmarks to share one :class:`~astrocyte.Astrocyte` instance."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
    monkeypatch.delenv("ASTROCYTE_CONFIG_PATH", raising=False)


def test_create_app_with_injected_brain_matches_direct_recall() -> None:
    from astrocyte_gateway.app import create_app
    from astrocyte_gateway.brain import build_astrocyte

    brain = build_astrocyte()
    app = create_app(brain)
    with TestClient(app) as client:
        r = client.post("/v1/recall", json={"query": "hello", "bank_id": "b1", "max_results": 3})
        assert r.status_code == 200
