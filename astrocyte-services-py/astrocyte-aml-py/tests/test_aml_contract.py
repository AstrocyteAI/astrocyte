"""Contract-conformance tests for the AML Add/Search adapter.

These pin the exact requirements from https://agentmemoryleaderboard.ai/api-guide.
A failure here means the submission would be rejected or mis-scored, so the
assertions deliberately mirror the spec's wording rather than our internals.

A fake brain stands in for Astrocyte: no DB, no LLM, no network — the suite
is safe to run alongside a live bench.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from astrocyte_aml.app import create_app, render_conversation, render_hit_content

# ── Fakes ────────────────────────────────────────────────────────────────


@dataclass
class _Hit:
    text: str
    score: float = 0.5
    memory_id: str | None = "mem_x"
    occurred_at: datetime | None = None
    retained_at: datetime | None = None
    metadata: dict | None = None


@dataclass
class _RetainResult:
    stored: bool = True
    deduplicated: bool = False
    error: str | None = None
    memory_id: str | None = "mem_1"


@dataclass
class _RecallResult:
    hits: list[_Hit] = field(default_factory=list)


class _FakeBrain:
    def __init__(self, hits: list[_Hit] | None = None, retain: _RetainResult | None = None):
        self._hits = hits if hits is not None else []
        self._retain = retain or _RetainResult()
        self.retain_calls: list[dict[str, Any]] = []
        self.recall_calls: list[dict[str, Any]] = []
        self.raise_on_retain: Exception | None = None
        self.raise_on_recall: Exception | None = None

    async def retain(self, content: str, **kwargs: Any) -> _RetainResult:
        if self.raise_on_retain:
            raise self.raise_on_retain
        self.retain_calls.append({"content": content, **kwargs})
        return self._retain

    async def recall(self, query: str, **kwargs: Any) -> _RecallResult:
        if self.raise_on_recall:
            raise self.raise_on_recall
        self.recall_calls.append({"query": query, **kwargs})
        return _RecallResult(hits=self._hits)


def _client(brain: _FakeBrain) -> TestClient:
    return TestClient(create_app(brain=brain), raise_server_exceptions=False)


ADD_BODY = {
    "request_id": "eval:run_abc123:locomo_refined:conv-0:chunk-0",
    "messages": [
        {"role": "user", "timestamp": 1704067200000, "content": "I adopted a beagle named Rex."},
        {"role": "assistant", "timestamp": 1704067260000, "content": "Congratulations!"},
    ],
    "user_id": "eval:run_abc123:locomo:conv-0",
    "session_id": "eval:run_abc123:sample:0",
}


# ── Add contract ─────────────────────────────────────────────────────────


class TestAddContract:
    def test_returns_success_and_echoes_all_three_ids(self):
        """Spec: success=true plus EXACT echo of request_id/user_id/session_id.
        A mismatch is an immediate failure regardless of HTTP 200."""
        c = _client(_FakeBrain())
        r = c.post("/add", json=ADD_BODY)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["request_id"] == ADD_BODY["request_id"]
        assert body["user_id"] == ADD_BODY["user_id"]
        assert body["session_id"] == ADD_BODY["session_id"]

    def test_user_id_is_the_isolation_boundary(self):
        """user_id must map to the storage isolation boundary (bank_id)."""
        brain = _FakeBrain()
        _client(brain).post("/add", json=ADD_BODY)
        assert brain.retain_calls[0]["bank_id"] == ADD_BODY["user_id"]

    def test_routes_through_conversation_engine(self):
        brain = _FakeBrain()
        _client(brain).post("/add", json=ADD_BODY)
        assert brain.retain_calls[0]["content_type"] == "conversation"

    def test_session_id_is_retained_as_metadata_not_boundary(self):
        brain = _FakeBrain()
        _client(brain).post("/add", json=ADD_BODY)
        call = brain.retain_calls[0]
        assert call["metadata"]["session_id"] == ADD_BODY["session_id"]
        assert call["bank_id"] != ADD_BODY["session_id"]

    def test_earliest_message_timestamp_anchors_domain_time(self):
        brain = _FakeBrain()
        _client(brain).post("/add", json=ADD_BODY)
        assert brain.retain_calls[0]["occurred_at"] == datetime.fromtimestamp(
            1704067200000 / 1000.0, tz=UTC,
        )

    def test_messages_without_timestamps_are_accepted(self):
        body = {**ADD_BODY, "messages": [{"role": "user", "content": "no stamp"}]}
        brain = _FakeBrain()
        r = _client(brain).post("/add", json=body)
        assert r.status_code == 200
        assert brain.retain_calls[0]["occurred_at"] is None

    def test_failed_persistence_returns_500_not_false_success(self):
        """Spec: 200 only after persistence AND searchability. A pipeline
        failure must surface as a retryable 500, never success=true."""
        brain = _FakeBrain(retain=_RetainResult(stored=False, error="vector store down"))
        r = _client(brain).post("/add", json=ADD_BODY)
        assert r.status_code == 500
        assert "vector store down" in r.json()["error"]

    def test_dedup_counts_as_stored_because_content_is_searchable(self):
        brain = _FakeBrain(retain=_RetainResult(stored=False, deduplicated=True))
        r = _client(brain).post("/add", json=ADD_BODY)
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_retain_exception_returns_retryable_500(self):
        brain = _FakeBrain()
        brain.raise_on_retain = RuntimeError("boom")
        assert _client(brain).post("/add", json=ADD_BODY).status_code == 500

    def test_empty_messages_is_a_non_retryable_400(self):
        """400/422 are in AML's do-not-retry set — correct for contract errors."""
        brain = _FakeBrain()
        r = _client(brain).post("/add", json={**ADD_BODY, "messages": []})
        assert r.status_code == 400
        assert brain.retain_calls == []

    def test_missing_required_field_is_422(self):
        body = {k: v for k, v in ADD_BODY.items() if k != "user_id"}
        assert _client(_FakeBrain()).post("/add", json=body).status_code == 422


# ── Search contract ──────────────────────────────────────────────────────


class TestSearchContract:
    def test_returns_data_array_with_required_id_and_content(self):
        brain = _FakeBrain(hits=[_Hit(text="Rex is a beagle", memory_id="m1", score=0.9)])
        r = _client(brain).post(
            "/search", json={"query": "What pet?", "user_id": "u1", "top_k": 100},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 1
        assert data[0]["id"] == "m1"
        assert data[0]["content"]
        assert data[0]["score"] == 0.9

    def test_empty_results_return_empty_array_not_error(self):
        r = _client(_FakeBrain(hits=[])).post(
            "/search", json={"query": "q", "user_id": "u1", "top_k": 100},
        )
        assert r.status_code == 200
        assert r.json()["data"] == []

    def test_search_scopes_to_user_id_only(self):
        brain = _FakeBrain()
        _client(brain).post("/search", json={"query": "q", "user_id": "u-42", "top_k": 100})
        assert brain.recall_calls[0]["bank_id"] == "u-42"

    def test_session_id_is_never_used_as_a_search_filter(self):
        """Spec: session_id is for grouping only, not a Search filter."""
        brain = _FakeBrain()
        _client(brain).post("/search", json={"query": "q", "user_id": "u1", "top_k": 100})
        assert brain.recall_calls[0].get("session_id") is None

    def test_result_cap_respects_dilution_evidence_under_top_k_100(self):
        """top_k is a maximum, not a quota. M30 evidence: answerer accuracy
        peaks near 50 candidates and degrades past it."""
        brain = _FakeBrain(hits=[_Hit(text=f"f{i}", memory_id=f"m{i}") for i in range(100)])
        r = _client(brain).post(
            "/search", json={"query": "q", "user_id": "u1", "top_k": 100},
        )
        assert len(r.json()["data"]) == 50

    def test_small_top_k_is_honoured_as_a_hard_ceiling(self):
        brain = _FakeBrain(hits=[_Hit(text=f"f{i}", memory_id=f"m{i}") for i in range(100)])
        r = _client(brain).post(
            "/search", json={"query": "q", "user_id": "u1", "top_k": 5},
        )
        assert len(r.json()["data"]) == 5

    def test_options_enrich_retrieval_without_altering_the_question(self):
        brain = _FakeBrain()
        _client(brain).post("/search", json={
            "query": "Which is right?", "user_id": "u1", "top_k": 10,
            "options": ["A. beagle", "B. poodle"],
        })
        q = brain.recall_calls[0]["query"]
        assert q.startswith("Which is right?")
        assert "beagle" in q and "poodle" in q

    def test_hits_with_empty_text_are_dropped_not_emitted_empty(self):
        """Spec: content must be a non-empty string."""
        brain = _FakeBrain(hits=[_Hit(text="", memory_id="m1"), _Hit(text="real", memory_id="m2")])
        data = _client(brain).post(
            "/search", json={"query": "q", "user_id": "u1", "top_k": 10},
        ).json()["data"]
        assert [d["id"] for d in data] == ["m2"]

    def test_ids_are_synthesised_when_missing(self):
        brain = _FakeBrain(hits=[_Hit(text="t", memory_id=None)])
        data = _client(brain).post(
            "/search", json={"query": "q", "user_id": "u1", "top_k": 10},
        ).json()["data"]
        assert data[0]["id"]

    def test_recall_exception_returns_retryable_500(self):
        brain = _FakeBrain()
        brain.raise_on_recall = RuntimeError("boom")
        r = _client(brain).post("/search", json={"query": "q", "user_id": "u1", "top_k": 10})
        assert r.status_code == 500

    def test_search_never_calls_reflect(self):
        """AML integrity rule: Search must not generate or disguise answers.
        The fake brain has no reflect(); calling it would AttributeError."""
        brain = _FakeBrain(hits=[_Hit(text="evidence")])
        assert not hasattr(brain, "reflect")
        r = _client(brain).post("/search", json={"query": "q", "user_id": "u1", "top_k": 10})
        assert r.status_code == 200


# ── Rendering ────────────────────────────────────────────────────────────


class TestRendering:
    def test_roles_and_iso_timestamps_are_inlined(self):
        from astrocyte_aml.app import AddMessage

        text = render_conversation([
            AddMessage(role="user", content="hi", timestamp=1704067200000),
            AddMessage(role="assistant", content="hello"),
        ])
        assert "**user**" in text and "**assistant**" in text
        assert "2024-01-01" in text  # timestamp anchored inline for retain-time resolution
        assert "hi" in text and "hello" in text

    def test_hit_content_inlines_temporal_context(self):
        """The platform's answerer sees ONLY this string — un-inlined
        metadata is lost information."""
        content = render_hit_content(
            _Hit(text="Adopted Rex", occurred_at=datetime(2024, 1, 1, tzinfo=UTC)),
        )
        assert "2024-01-01" in content
        assert "Adopted Rex" in content

    def test_hit_content_distinguishes_occurred_from_recorded(self):
        content = render_hit_content(_Hit(
            text="Adopted Rex",
            occurred_at=datetime(2024, 1, 1, tzinfo=UTC),
            retained_at=datetime(2024, 6, 1, tzinfo=UTC),
        ))
        assert "occurred 2024-01-01" in content
        assert "recorded 2024-06-01" in content

    def test_hit_content_includes_speaker_when_known(self):
        content = render_hit_content(_Hit(text="I like jazz", metadata={"speaker": "user"}))
        assert "(user)" in content

    def test_hit_content_is_plain_text_when_no_metadata(self):
        assert render_hit_content(_Hit(text="bare fact")) == "bare fact"


# ── Ops ──────────────────────────────────────────────────────────────────


class TestOps:
    def test_health_is_unauthenticated_2xx(self):
        r = _client(_FakeBrain()).get("/health")
        assert r.status_code == 200

    def test_api_key_rejects_with_non_retryable_401(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_AML_API_KEY", "secret")
        c = _client(_FakeBrain())
        assert c.post("/search", json={"query": "q", "user_id": "u", "top_k": 5}).status_code == 401

    @pytest.mark.parametrize("headers", [
        {"X-Api-Key": "secret"},
        {"Authorization": "Bearer secret"},
        {"Authorization": "Token secret"},
    ])
    def test_all_three_auth_schemes_are_accepted(self, monkeypatch, headers):
        monkeypatch.setenv("ASTROCYTE_AML_API_KEY", "secret")
        r = _client(_FakeBrain()).post(
            "/search", json={"query": "q", "user_id": "u", "top_k": 5}, headers=headers,
        )
        assert r.status_code == 200

    def test_health_stays_open_when_auth_is_configured(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_AML_API_KEY", "secret")
        assert _client(_FakeBrain()).get("/health").status_code == 200
