"""POST /v1/retain keeps when and where a memory came from (they were dropped)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    for name in ("ASTROCYTE_ADMIN_TOKEN", "ASTROCYTE_RATE_LIMIT_PER_SECOND", "ASTROCYTE_TOKENS_FILE"):
        monkeypatch.delenv(name, raising=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "provider_tier: storage\nvector_store: in_memory\nllm_provider: mock\n"
        "barriers: { pii: { mode: disabled } }\naccess_control: { enabled: false }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
    from astrocyte_gateway.app import create_app

    return TestClient(create_app())


def _recall(client: TestClient, query: str) -> list[dict]:
    r = client.post("/v1/recall", json={"query": query, "bank_id": "b1"})
    assert r.status_code == 200, r.text
    return r.json()["hits"]


def test_retain_keeps_occurred_at_and_source(client):
    r = client.post("/v1/retain", json={
        "content": "The staging deploy freeze moved to Thursdays.", "bank_id": "b1",
        "occurred_at": "2026-09-30T14:00:00Z", "source": "https://wiki.example.com/deploys#freeze"})
    assert r.status_code == 200 and r.json()["stored"], r.text
    [hit] = [h for h in _recall(client, "deploy freeze") if "Thursdays" in h["text"]]
    assert hit["source"] == "https://wiki.example.com/deploys#freeze"
    assert hit["occurred_at"].startswith("2026-09-30T14:00:00")


def test_a_naive_occurred_at_is_utc(client):
    client.post("/v1/retain", json={"content": "Billing runs on Go.", "bank_id": "b1",
                                    "occurred_at": "2026-09-01T08:30:00"})
    [hit] = [h for h in _recall(client, "billing language") if "Go" in h["text"]]
    assert hit["occurred_at"].startswith("2026-09-01T08:30:00") and hit["occurred_at"].endswith(("+00:00", "Z"))


def test_both_stay_optional(client):
    r = client.post("/v1/retain", json={"content": "We use httpx.", "bank_id": "b1"})
    assert r.status_code == 200 and r.json()["stored"]
    [hit] = [h for h in _recall(client, "http client") if "httpx" in h["text"]]
    assert hit["source"] is None


@pytest.mark.parametrize("body", [{"occurred_at": "not a time"}, {"source": "x" * 2049}])
def test_bad_values_are_400(client, body):
    r = client.post("/v1/retain", json={"content": "c", "bank_id": "b1", **body})
    assert r.status_code == 400, r.text
