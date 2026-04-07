"""Tests for REST endpoint input validation and scope support."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTROCYTES_AUTH_MODE", "dev")
    monkeypatch.delenv("ASTROCYTES_CONFIG_PATH", raising=False)


def _client() -> TestClient:
    from astrocyte_rest.app import create_app

    return TestClient(create_app())


# ---------------------------------------------------------------------------
# /v1/recall — require bank_id or banks
# ---------------------------------------------------------------------------


class TestRecallValidation:
    def test_recall_without_bank_returns_400(self):
        client = _client()
        r = client.post("/v1/recall", json={"query": "hello"})
        assert r.status_code == 400
        assert "bank_id or banks" in r.json()["detail"]

    def test_recall_with_bank_id_succeeds(self):
        client = _client()
        r = client.post("/v1/recall", json={"query": "hello", "bank_id": "b1"})
        assert r.status_code == 200

    def test_recall_with_banks_succeeds(self):
        client = _client()
        r = client.post("/v1/recall", json={"query": "hello", "banks": ["b1", "b2"]})
        assert r.status_code == 200

    def test_recall_non_numeric_max_results_returns_400(self):
        client = _client()
        r = client.post(
            "/v1/recall",
            json={"query": "hello", "bank_id": "b1", "max_results": "abc"},
        )
        assert r.status_code == 400
        assert "max_results" in r.json()["detail"]

    def test_recall_non_numeric_max_tokens_returns_400(self):
        client = _client()
        r = client.post(
            "/v1/recall",
            json={"query": "hello", "bank_id": "b1", "max_tokens": "xyz"},
        )
        assert r.status_code == 400
        assert "max_tokens" in r.json()["detail"]

    def test_recall_numeric_string_max_results_succeeds(self):
        client = _client()
        r = client.post(
            "/v1/recall",
            json={"query": "hello", "bank_id": "b1", "max_results": "5"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# /v1/reflect — max_tokens validation
# ---------------------------------------------------------------------------


class TestReflectValidation:
    def test_reflect_non_numeric_max_tokens_returns_400(self):
        client = _client()
        r = client.post(
            "/v1/reflect",
            json={"query": "hello", "bank_id": "b1", "max_tokens": "bad"},
        )
        assert r.status_code == 400
        assert "max_tokens" in r.json()["detail"]


# ---------------------------------------------------------------------------
# /v1/forget — scope support
# ---------------------------------------------------------------------------


class TestForgetScope:
    def test_forget_scope_all_succeeds(self):
        client = _client()
        # First retain something
        client.post(
            "/v1/retain",
            json={"content": "test memory", "bank_id": "forget-scope-bank"},
        )
        # Forget with scope=all
        r = client.post(
            "/v1/forget",
            json={"bank_id": "forget-scope-bank", "scope": "all"},
        )
        assert r.status_code == 200
        assert r.json()["deleted_count"] >= 0

    def test_forget_invalid_scope_returns_400(self):
        client = _client()
        r = client.post(
            "/v1/forget",
            json={"bank_id": "b1", "scope": "invalid"},
        )
        assert r.status_code == 400
        assert "scope" in r.json()["detail"]

    def test_forget_without_scope_succeeds(self):
        client = _client()
        r = client.post(
            "/v1/forget",
            json={"bank_id": "b1", "memory_ids": ["nonexistent"]},
        )
        assert r.status_code == 200
