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


def test_openapi_matches_checked_in_snapshot() -> None:
    """The live schema must equal openapi.json (the HTTP contract artifact).

    Any route, method, request-model, or response change shows up as a diff
    here. If the change is INTENTIONAL, regenerate and commit the snapshot:

        uv run python scripts/generate_openapi.py

    CI's oasdiff job then classifies the snapshot diff as breaking or
    non-breaking against the base branch.
    """
    import json
    import pathlib

    from astrocyte_gateway.app import create_app

    snapshot_path = pathlib.Path(__file__).resolve().parent.parent / "openapi.json"
    assert snapshot_path.exists(), "openapi.json snapshot missing — run scripts/generate_openapi.py"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    live = json.loads(json.dumps(create_app().openapi(), sort_keys=True))

    if live != snapshot:
        live_paths, snap_paths = set(live.get("paths", {})), set(snapshot.get("paths", {}))
        hint = ""
        if live_paths != snap_paths:
            hint = f" Added paths: {sorted(live_paths - snap_paths)}; removed: {sorted(snap_paths - live_paths)}."
        raise AssertionError(
            "Live OpenAPI schema differs from the checked-in openapi.json snapshot."
            f"{hint} If intentional, run scripts/generate_openapi.py and commit the result."
        )
