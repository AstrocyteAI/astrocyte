"""OpenAPI schema contract: Tier 1 routes remain discoverable and documented."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
    monkeypatch.delenv("ASTROCYTE_CONFIG_PATH", raising=False)
    monkeypatch.delenv("ASTROCYTE_RATE_LIMIT_PER_SECOND", raising=False)


def test_openapi_paths_include_v1_memory_routes() -> None:
    from astrocyte_gateway.app import create_app

    app = create_app()
    schema = app.openapi()
    paths = schema.get("paths") or {}
    for route in (
        "/v1/retain",
        "/v1/recall",
        "/v1/debug/recall",
        "/v1/reflect",
        "/v1/mental-models",
        "/v1/mental-models/{model_id}",
        "/v1/mental-models/{model_id}/refresh",
        "/v1/observations/invalidate",
        "/v1/forget",
        "/v1/ingest/webhook/{source_id}",
        "/v1/admin/sources",
        "/v1/admin/banks",
        "/health",
        "/health/ingest",
        "/live",
    ):
        assert route in paths, f"missing OpenAPI path {route}"

    assert paths["/v1/retain"].get("post") is not None
    assert paths["/v1/recall"].get("post") is not None


def test_openapi_served_at_docs_and_json() -> None:
    from astrocyte_gateway.app import create_app

    with TestClient(create_app()) as client:
        r = client.get("/openapi.json")
        assert r.status_code == 200
        body = r.json()
        assert "/v1/recall" in body.get("paths", {})
