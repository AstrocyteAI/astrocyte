"""Tests for the AML self-evaluation retrieval bridge.

Runs entirely against the in-process adapter (fake brain) via ASGI
transport — no server, no DB, no network, no LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from aml_selfeval.retrieve import (
    MAX_MESSAGES_PER_ADD,
    batch_messages,
    ingest_item,
    load_locomo,
    load_longmemeval,
    search_item,
    to_aml_record,
)
from astrocyte_aml.app import create_app


@dataclass
class _Hit:
    text: str
    score: float = 0.5
    memory_id: str = "m1"
    occurred_at: Any = None
    retained_at: Any = None
    metadata: dict | None = None


@dataclass
class _RetainResult:
    stored: bool = True
    deduplicated: bool = False
    error: str | None = None


@dataclass
class _RecallResult:
    hits: list[_Hit] = field(default_factory=list)


class _FakeBrain:
    def __init__(self, hits: list[_Hit] | None = None):
        self._hits = hits or []
        self.retain_calls: list[dict[str, Any]] = []
        self.recall_calls: list[dict[str, Any]] = []

    async def retain(self, content: str, **kw: Any) -> _RetainResult:
        self.retain_calls.append({"content": content, **kw})
        return _RetainResult()

    async def recall(self, query: str, **kw: Any) -> _RecallResult:
        self.recall_calls.append({"query": query, **kw})
        return _RecallResult(hits=self._hits)


def _client(brain: _FakeBrain) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(brain=brain)),
        base_url="http://test",
    )


class TestAddBatching:
    def test_splits_on_message_count_limit(self):
        turns = [{"role": "user", "content": "x"} for _ in range(45)]
        batches = batch_messages(turns)
        assert all(len(b) <= MAX_MESSAGES_PER_ADD for b in batches)
        assert sum(len(b) for b in batches) == 45

    def test_splits_on_word_budget(self):
        turns = [{"role": "user", "content": " ".join(["w"] * 500)} for _ in range(10)]
        batches = batch_messages(turns)
        assert len(batches) > 1
        assert sum(len(b) for b in batches) == 10

    def test_single_oversized_turn_is_not_dropped(self):
        turns = [{"role": "user", "content": " ".join(["w"] * 5000)}]
        assert sum(len(b) for b in batch_messages(turns)) == 1

    def test_empty_input_yields_no_batches(self):
        assert batch_messages([]) == []


class TestDatasetLoaders:
    def test_longmemeval_normalizes_sessions_and_gold(self, tmp_path):
        src = tmp_path / "lme.json"
        src.write_text(json.dumps([{
            "question_id": "q1", "question": "What pet?", "answer": "a beagle",
            "question_type": "single-session-user",
            "haystack_sessions": [
                [{"role": "user", "content": "I got a beagle"}, {"role": "assistant", "content": "Nice"}],
                [{"role": "user", "content": ""}],  # empty turns dropped
            ],
        }]))
        items = load_longmemeval(src)
        assert len(items) == 1
        assert items[0]["id"] == "q1"
        assert items[0]["gold_answer"] == "a beagle"
        assert len(items[0]["sessions"]) == 1  # the empty-turn session is dropped
        assert len(items[0]["sessions"][0]) == 2

    def test_locomo_expands_one_item_per_question(self, tmp_path):
        src = tmp_path / "locomo.json"
        src.write_text(json.dumps([{
            "conversation": {
                "session_1": [{"speaker": "Alice", "text": "hi"}],
                "session_1_date_time": "1 Jan 2024",
                "session_2": [{"speaker": "Bob", "text": "hello"}],
            },
            "qa": [
                {"question": "Q1", "answer": "A1", "category": 1},
                {"question": "Q2", "answer": "A2", "category": 2},
                {"question": "adversarial"},  # no gold — skipped
            ],
        }]))
        items = load_locomo(src)
        assert len(items) == 2
        assert {i["id"] for i in items} == {"conv0-q0", "conv0-q1"}
        # date keys must not be treated as sessions
        assert len(items[0]["sessions"]) == 2


class TestAmlRecord:
    def test_populates_the_fallback_key_the_prompt_reads(self):
        """AML's render_answer_prompt falls back to retrieved_context."""
        rec = to_aml_record(
            {"id": "q1", "question": "Q", "gold_answer": "G", "question_type": "t"},
            [{"content": "fact one"}, {"content": "fact two"}],
        )
        assert "fact one" in rec["retrieved_context"]
        assert "fact two" in rec["retrieved_context"]
        assert rec["n_retrieved"] == 2

    def test_gold_answer_key_matches_pipeline_expectation(self):
        """AML's gold_answer() accepts gold_answer/golden_answer/..."""
        rec = to_aml_record({"id": "1", "question": "Q", "gold_answer": "G"}, [])
        assert rec["gold_answer"] == "G"

    def test_empty_retrieval_is_explicit_not_blank(self):
        rec = to_aml_record({"id": "1", "question": "Q", "gold_answer": "G"}, [])
        assert rec["retrieved_context"].strip()
        assert rec["n_retrieved"] == 0

    def test_hits_without_content_are_skipped(self):
        rec = to_aml_record(
            {"id": "1", "question": "Q", "gold_answer": "G"},
            [{"content": ""}, {"content": "real"}],
        )
        assert rec["n_retrieved"] == 1


class TestDriverAgainstAdapter:
    @pytest.mark.asyncio
    async def test_ingest_scopes_every_add_to_the_item_user_id(self):
        brain = _FakeBrain()
        item = {"id": "q1", "sessions": [[{"role": "user", "content": "a"}],
                                         [{"role": "user", "content": "b"}]]}
        async with _client(brain) as c:
            sent = await ingest_item(c, "", "run1", item)
        assert sent == 2
        banks = {call["bank_id"] for call in brain.retain_calls}
        assert banks == {"selfeval:run1:q1"}

    @pytest.mark.asyncio
    async def test_each_session_gets_a_distinct_session_id(self):
        brain = _FakeBrain()
        item = {"id": "q1", "sessions": [[{"role": "user", "content": "a"}],
                                         [{"role": "user", "content": "b"}]]}
        async with _client(brain) as c:
            await ingest_item(c, "", "run1", item)
        sids = {call["metadata"]["session_id"] for call in brain.retain_calls}
        assert sids == {"selfeval:run1:q1:s0", "selfeval:run1:q1:s1"}

    @pytest.mark.asyncio
    async def test_search_uses_the_same_isolation_scope_as_add(self):
        """AML requires identical user_id across Add and Search."""
        brain = _FakeBrain(hits=[_Hit(text="found it")])
        item = {"id": "q1", "question": "Q?", "sessions": [[{"role": "user", "content": "a"}]]}
        async with _client(brain) as c:
            await ingest_item(c, "", "run1", item)
            hits = await search_item(c, "", "run1", item)
        assert brain.recall_calls[0]["bank_id"] == brain.retain_calls[0]["bank_id"]
        assert hits and "found it" in hits[0]["content"]

    @pytest.mark.asyncio
    async def test_requests_aml_top_k_of_100(self):
        brain = _FakeBrain(hits=[_Hit(text="x")])
        async with _client(brain) as c:
            await search_item(c, "", "run1", {"id": "q1", "question": "Q?"})
        # adapter fetches at least the AML top_k before applying its own cap
        assert brain.recall_calls[0]["max_results"] >= 100

    @pytest.mark.asyncio
    async def test_end_to_end_produces_a_scorable_record(self):
        brain = _FakeBrain(hits=[_Hit(text="Rex is a beagle")])
        item = {"id": "q1", "question": "What pet?", "gold_answer": "beagle",
                "question_type": "ssu", "sessions": [[{"role": "user", "content": "I got Rex"}]]}
        async with _client(brain) as c:
            await ingest_item(c, "", "run1", item)
            hits = await search_item(c, "", "run1", item)
        rec = to_aml_record(item, hits)
        assert {"id", "question", "gold_answer", "retrieved_context"} <= set(rec)
        assert "beagle" in rec["retrieved_context"]
