"""Structured fact extraction must reach the stored rows.

Until 2026-10-04 retain() kept only each chunk's text (and entities) from
structured extraction: the per-chunk classification, event time, and
when/where/who metadata were computed by a paid LLM call and then discarded.
These tests pin that every extracted field lands on the row of the chunk it
describes, through dedup and across a restart, and that a field the model did
not supply never overrides the request's own value.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.pipeline.retain_stage import _apply_fact_overlay, _FactOverlay
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import Completion, RetainRequest

SAID_AT = datetime(2023, 9, 1, tzinfo=UTC)

# Keyed by a word unique to each paragraph, so the fake model answers each
# chunk by its content, whatever subset of chunks it is shown.
META = {
    "Paris": {
        "fact_type": "experience",
        "when": "June 2023",
        "where": "Paris",
        "who": "Alice",
        "occurred_start": "2023-06-01T00:00:00+00:00",
    },
    "boils": {"fact_type": "world", "when": "N/A", "where": "N/A", "who": "N/A"},
    "Tokyo": {
        "fact_type": "experience",
        "when": "March 2022",
        "where": "Tokyo",
        "who": "Carol",
        "occurred_start": "2022-03-10T00:00:00+00:00",
    },
}
PARAS = [
    "Alice moved to Paris last June for work.",
    "Water boils at one hundred degrees.",
    "Carol flew to Tokyo in March last year.",
]
CONTENT = "\n\n".join(PARAS)


class _ExtractingLLM(MockLLMProvider):
    """Answers the verbatim-extraction prompt per chunk; ``skip`` words get
    no entry, reproducing a model that drops a chunk."""

    def __init__(self, skip: frozenset[str] = frozenset()):
        super().__init__()
        self.skip = skip
        self.extraction_calls = 0

    async def complete(self, messages, **kwargs):
        prompt = messages[-1].content if messages else ""
        if "each with its chunk_index" not in prompt:
            return await super().complete(messages, **kwargs)
        self.extraction_calls += 1
        facts = []
        for idx, text in re.findall(r"^\[(\d+)\] (.*)$", prompt, flags=re.M):
            key = next((k for k in META if k in text), None)
            if key is None or key in self.skip:
                continue
            facts.append({"chunk_index": int(idx), **META[key], "entities": []})
        return Completion(text=json.dumps({"facts": facts}), model="fake", usage=None)


def _orch(store: InMemoryVectorStore, llm: _ExtractingLLM) -> PipelineOrchestrator:
    orch = PipelineOrchestrator(store, llm)
    orch.structured_fact_extraction_enabled = True
    # One paragraph per chunk: short enough that the paragraph chunker
    # never merges two of them.
    orch.structured_fact_extraction_chunk_max_size = 60
    return orch


async def _rows_by_word(store: InMemoryVectorStore) -> dict[str, object]:
    rows = await store.list_vectors("b1")
    return {next(k for k in META if k in r.text): r for r in rows}


@pytest.mark.asyncio
async def test_extracted_fields_reach_the_stored_row():
    store = InMemoryVectorStore()
    r = await _orch(store, _ExtractingLLM()).retain(RetainRequest(content=CONTENT, bank_id="b1", occurred_at=SAID_AT))
    assert r.stored
    rows = await _rows_by_word(store)
    assert set(rows) == {"Paris", "boils", "Tokyo"}

    paris = rows["Paris"]
    assert paris.fact_type == "experience"
    assert paris.occurred_at == datetime(2023, 6, 1, tzinfo=UTC), "event time, not the request's"
    assert paris.metadata["_fact_where"] == "Paris"
    assert paris.metadata["_fact_who"] == "Alice"
    assert paris.metadata["_mentioned_at"] == SAID_AT.isoformat(), "request time kept, not lost"

    boils = rows["boils"]
    assert boils.fact_type == "world"
    assert boils.occurred_at == SAID_AT, "no extracted event time: request time stands"
    assert "_mentioned_at" not in boils.metadata


@pytest.mark.asyncio
async def test_a_chunk_the_model_skipped_keeps_the_request_values():
    """A skipped chunk gets nothing: not a default classification, and not a
    neighbour's fields."""
    store = InMemoryVectorStore()
    await _orch(store, _ExtractingLLM(skip=frozenset({"boils"}))).retain(
        RetainRequest(content=CONTENT, bank_id="b1", occurred_at=SAID_AT)
    )
    rows = await _rows_by_word(store)
    boils = rows["boils"]
    assert boils.fact_type == "world", "the default, not extraction's 'experience' fallback"
    assert boils.occurred_at == SAID_AT
    assert not any(k.startswith("_fact_") for k in boils.metadata)
    assert rows["Tokyo"].metadata["_fact_where"] == "Tokyo", "later chunks not shifted"


@pytest.mark.asyncio
async def test_partial_ingest_then_retry_after_restart_aligns_metadata():
    """The AML failure shape: an Add is half-stored, the process restarts,
    and the full batch is retried. Dedup drops the stored chunk, and the
    surviving chunks must carry their OWN metadata, not a shifted neighbour's."""
    store = InMemoryVectorStore()
    await _orch(store, _ExtractingLLM()).retain(RetainRequest(content=PARAS[0], bank_id="b1", occurred_at=SAID_AT))
    retry = await _orch(store, _ExtractingLLM()).retain(  # fresh process: empty dedup cache
        RetainRequest(content=CONTENT, bank_id="b1", occurred_at=SAID_AT)
    )
    assert retry.stored
    rows = await store.list_vectors("b1")
    assert len(rows) == 3, "the already-stored chunk is not stored twice"
    by_word = await _rows_by_word(store)
    assert by_word["boils"].fact_type == "world"
    assert "_fact_where" not in by_word["boils"].metadata
    assert by_word["Tokyo"].metadata["_fact_where"] == "Tokyo"
    assert by_word["Tokyo"].occurred_at == datetime(2022, 3, 10, tzinfo=UTC)


@pytest.mark.asyncio
async def test_retain_many_persists_extracted_fields_too():
    store = InMemoryVectorStore()
    results = await _orch(store, _ExtractingLLM()).retain_many(
        [RetainRequest(content=CONTENT, bank_id="b1", occurred_at=SAID_AT)]
    )
    assert results[0].stored
    rows = await _rows_by_word(store)
    assert rows["Paris"].occurred_at == datetime(2023, 6, 1, tzinfo=UTC)
    assert rows["Paris"].metadata["_fact_where"] == "Paris"
    assert rows["boils"].fact_type == "world"


class TestOverlayPrecedence:
    def test_explicit_profile_fact_type_beats_the_model(self):
        _, fact_type, _ = _apply_fact_overlay(
            None,
            _FactOverlay(metadata={}, fact_type="experience", occurred_at=None),
            fact_type="world",
            occurred_at=None,
            profile_fact_type="world",
        )
        assert fact_type == "world"

    def test_caller_metadata_is_never_overwritten(self):
        meta, _, _ = _apply_fact_overlay(
            {"_fact_where": "caller says Rome", "k": 1},
            _FactOverlay(metadata={"_fact_where": "Paris"}, fact_type=None, occurred_at=None),
            fact_type="world",
            occurred_at=None,
            profile_fact_type=None,
        )
        assert meta == {"_fact_where": "caller says Rome", "k": 1}

    def test_no_overlay_changes_nothing(self):
        assert _apply_fact_overlay({"k": 1}, None, fact_type="world", occurred_at=SAID_AT, profile_fact_type=None) == (
            {"k": 1},
            "world",
            SAID_AT,
        )

    def test_event_time_without_a_request_time_adds_no_mentioned_at(self):
        meta, _, occurred = _apply_fact_overlay(
            None,
            _FactOverlay(metadata={}, fact_type=None, occurred_at=SAID_AT),
            fact_type="world",
            occurred_at=None,
            profile_fact_type=None,
        )
        assert occurred == SAID_AT
        assert not meta
