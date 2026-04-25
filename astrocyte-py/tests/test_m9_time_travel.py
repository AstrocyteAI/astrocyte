"""M9: Time travel — unit and integration tests.

Tests cover:
- VectorItem.retained_at field exists and is optional
- VectorFilters.as_of field exists
- RecallRequest.as_of field exists
- HistoryResult type fields
- InMemoryVectorStore.search_similar: as_of filter excludes future items
- InMemoryVectorStore.search_similar: as_of=None → no filtering (backward compat)
- Items with retained_at=None are not excluded by as_of (no timestamp = always visible)
- Orchestrator.retain stamps retained_at on each VectorItem
- Orchestrator.recall passes as_of from RecallRequest into VectorFilters
- brain.recall(as_of=...) surfaces only historically visible memories
- brain.history() convenience wrapper:
    - Returns HistoryResult with correct as_of and bank_id
    - Excludes memories retained after as_of
    - Includes memories retained before as_of
    - Empty result when all memories are in the future
- retained_at flows through to MemoryHit.retained_at in recall results
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import (
    HistoryResult,
    RecallRequest,
    RetainRequest,
    VectorFilters,
    VectorItem,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIM = 16


def _unit_vec(pos: int) -> list[float]:
    v = [0.0] * _DIM
    v[pos % _DIM] = 1.0
    return v


def _ts(days_ago: float) -> datetime:
    """Return a UTC datetime that many days in the past."""
    return datetime.now(UTC) - timedelta(days=days_ago)


def _future(days: float = 1.0) -> datetime:
    """Return a UTC datetime that many days in the future."""
    return datetime.now(UTC) + timedelta(days=days)


def _item(
    item_id: str,
    bank_id: str,
    text: str = "memory text",
    retained_at: datetime | None = None,
    vec_pos: int = 0,
) -> VectorItem:
    return VectorItem(
        id=item_id,
        bank_id=bank_id,
        vector=_unit_vec(vec_pos),
        text=text,
        retained_at=retained_at,
    )


# ---------------------------------------------------------------------------
# Type-level tests
# ---------------------------------------------------------------------------


class TestTypeFields:
    def test_vector_item_has_retained_at(self):
        item = VectorItem(id="x", bank_id="b", vector=[1.0], text="t")
        assert hasattr(item, "retained_at")
        assert item.retained_at is None

    def test_vector_filters_has_as_of(self):
        f = VectorFilters()
        assert hasattr(f, "as_of")
        assert f.as_of is None

    def test_recall_request_has_as_of(self):
        r = RecallRequest(query="q", bank_id="b")
        assert hasattr(r, "as_of")
        assert r.as_of is None

    def test_history_result_fields(self):
        ts = datetime.now(UTC)
        r = HistoryResult(
            hits=[],
            total_available=0,
            truncated=False,
            as_of=ts,
            bank_id="bank1",
        )
        assert r.as_of is ts
        assert r.bank_id == "bank1"
        assert r.trace is None


# ---------------------------------------------------------------------------
# InMemoryVectorStore as_of filtering
# ---------------------------------------------------------------------------


class TestVectorStoreAsOf:
    @pytest.mark.asyncio
    async def test_no_filter_returns_all(self):
        vs = InMemoryVectorStore()
        old = _item("m1", "b", retained_at=_ts(10))
        new = _item("m2", "b", retained_at=_ts(1))
        await vs.store_vectors([old, new])

        hits = await vs.search_similar(_unit_vec(0), "b", limit=10)
        assert {h.id for h in hits} == {"m1", "m2"}

    @pytest.mark.asyncio
    async def test_as_of_excludes_future_items(self):
        vs = InMemoryVectorStore()
        past = _item("m-past", "b", retained_at=_ts(5))
        future = _item("m-future", "b", retained_at=_future(1))
        await vs.store_vectors([past, future])

        # as_of = now → should exclude the future item
        filters = VectorFilters(as_of=datetime.now(UTC))
        hits = await vs.search_similar(_unit_vec(0), "b", limit=10, filters=filters)
        ids = {h.id for h in hits}
        assert "m-past" in ids
        assert "m-future" not in ids

    @pytest.mark.asyncio
    async def test_as_of_point_in_past(self):
        vs = InMemoryVectorStore()
        very_old = _item("m-old", "b", retained_at=_ts(30))
        recent = _item("m-recent", "b", retained_at=_ts(2))
        await vs.store_vectors([very_old, recent])

        # as_of = 7 days ago → only very_old visible
        filters = VectorFilters(as_of=_ts(7))
        hits = await vs.search_similar(_unit_vec(0), "b", limit=10, filters=filters)
        ids = {h.id for h in hits}
        assert "m-old" in ids
        assert "m-recent" not in ids

    @pytest.mark.asyncio
    async def test_item_without_retained_at_always_visible(self):
        """Items with no retained_at are treated as always-present."""
        vs = InMemoryVectorStore()
        no_ts = _item("m-nots", "b", retained_at=None)
        await vs.store_vectors([no_ts])

        # Even with a very old as_of, item with no timestamp is included
        filters = VectorFilters(as_of=_ts(365))
        hits = await vs.search_similar(_unit_vec(0), "b", limit=10, filters=filters)
        assert any(h.id == "m-nots" for h in hits)

    @pytest.mark.asyncio
    async def test_as_of_exact_boundary_inclusive(self):
        """Items retained exactly at as_of are included (<=, not <)."""
        vs = InMemoryVectorStore()
        exact_ts = _ts(5)
        item = _item("m-exact", "b", retained_at=exact_ts)
        await vs.store_vectors([item])

        filters = VectorFilters(as_of=exact_ts)
        hits = await vs.search_similar(_unit_vec(0), "b", limit=10, filters=filters)
        assert any(h.id == "m-exact" for h in hits)

    @pytest.mark.asyncio
    async def test_retained_at_propagated_to_vector_hit(self):
        vs = InMemoryVectorStore()
        ts = _ts(3)
        item = _item("m1", "b", retained_at=ts)
        await vs.store_vectors([item])

        hits = await vs.search_similar(_unit_vec(0), "b", limit=10)
        assert hits[0].retained_at == ts

    @pytest.mark.asyncio
    async def test_bank_isolation_respected_with_as_of(self):
        vs = InMemoryVectorStore()
        b1 = _item("m1", "bank1", retained_at=_ts(1))
        b2 = _item("m2", "bank2", retained_at=_ts(1))
        await vs.store_vectors([b1, b2])

        filters = VectorFilters(as_of=datetime.now(UTC))
        hits = await vs.search_similar(_unit_vec(0), "bank1", limit=10, filters=filters)
        assert all(h.id == "m1" for h in hits)


# ---------------------------------------------------------------------------
# Orchestrator: retained_at stamped at retain time
# ---------------------------------------------------------------------------


class TestOrchestratorRetainedAt:
    @pytest.mark.asyncio
    async def test_store_vectors_gets_retained_at(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        before = datetime.now(UTC)
        await orch.retain(RetainRequest(content="Alice works at Meta.", bank_id="bank1"))
        after = datetime.now(UTC)

        items = await vs.list_vectors("bank1")
        assert len(items) >= 1
        for item in items:
            assert item.retained_at is not None
            assert before <= item.retained_at <= after

    @pytest.mark.asyncio
    async def test_retained_at_in_memory_hit(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        await orch.retain(RetainRequest(content="Alice works at Meta.", bank_id="bank1"))
        result = await orch.recall(RecallRequest(query="Alice", bank_id="bank1"))

        assert result.hits
        # retained_at propagated to MemoryHit
        for hit in result.hits:
            assert hit.retained_at is not None


# ---------------------------------------------------------------------------
# Orchestrator: as_of filter in recall
# ---------------------------------------------------------------------------


class TestOrchestratorAsOf:
    @pytest.mark.asyncio
    async def test_as_of_excludes_recently_retained(self):
        """Memories retained after as_of must not appear in recall results."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        checkpoint = datetime.now(UTC)

        # Retain memory BEFORE checkpoint — inject directly with controlled retained_at
        old = VectorItem(
            id="old-mem",
            bank_id="bank1",
            vector=[1.0] + [0.0] * (_DIM - 1),
            text="Alice works at Meta.",
            retained_at=checkpoint - timedelta(hours=1),
        )
        new = VectorItem(
            id="new-mem",
            bank_id="bank1",
            vector=[1.0] + [0.0] * (_DIM - 1),
            text="Alice now works at Google.",
            retained_at=checkpoint + timedelta(hours=1),
        )
        await vs.store_vectors([old, new])

        # Recall as_of checkpoint — should only see old-mem
        result = await orch.recall(
            RecallRequest(query="Alice", bank_id="bank1", as_of=checkpoint)
        )
        ids = {h.memory_id for h in result.hits}
        assert "old-mem" in ids
        assert "new-mem" not in ids

    @pytest.mark.asyncio
    async def test_as_of_none_returns_all(self):
        """Without as_of, all memories are returned (backward compatibility)."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        await vs.store_vectors([
            _item("m1", "bank1", "Alice at Meta", retained_at=_ts(5)),
            _item("m2", "bank1", "Alice at Google", retained_at=_future(1)),
        ])

        result = await orch.recall(RecallRequest(query="Alice", bank_id="bank1", as_of=None))
        ids = {h.memory_id for h in result.hits}
        assert "m1" in ids
        assert "m2" in ids


# ---------------------------------------------------------------------------
# brain.history()
# ---------------------------------------------------------------------------


class TestBrainHistory:
    def _brain(self) -> tuple[Astrocyte, InMemoryVectorStore]:
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        brain = Astrocyte(AstrocyteConfig())
        brain.set_pipeline(PipelineOrchestrator(vs, llm))
        return brain, vs

    @pytest.mark.asyncio
    async def test_history_returns_history_result(self):
        brain, vs = self._brain()
        await vs.store_vectors([_item("m1", "bank1", "Alice at Meta", retained_at=_ts(5))])

        ts = datetime.now(UTC)
        result = await brain.history("Alice", bank_id="bank1", as_of=ts)

        assert isinstance(result, HistoryResult)
        assert result.as_of == ts
        assert result.bank_id == "bank1"

    @pytest.mark.asyncio
    async def test_history_excludes_future_memories(self):
        brain, vs = self._brain()

        checkpoint = _ts(3)  # 3 days ago
        await vs.store_vectors([
            _item("m-old", "bank1", "Alice at Meta", retained_at=_ts(10)),
            _item("m-new", "bank1", "Alice at Google", retained_at=_ts(1)),  # more recent
        ])

        # as_of 3 days ago → only m-old visible
        result = await brain.history("Alice", bank_id="bank1", as_of=checkpoint)
        ids = {h.memory_id for h in result.hits}
        assert "m-old" in ids
        assert "m-new" not in ids

    @pytest.mark.asyncio
    async def test_history_includes_memories_retained_before_as_of(self):
        brain, vs = self._brain()

        await vs.store_vectors([
            _item("m1", "bank1", "Alice fact one", retained_at=_ts(5)),
            _item("m2", "bank1", "Alice fact two", retained_at=_ts(2)),
        ])

        # as_of = now → both visible
        result = await brain.history("Alice", bank_id="bank1", as_of=datetime.now(UTC))
        ids = {h.memory_id for h in result.hits}
        assert "m1" in ids
        assert "m2" in ids

    @pytest.mark.asyncio
    async def test_history_empty_when_all_memories_in_future(self):
        brain, vs = self._brain()

        await vs.store_vectors([
            _item("m1", "bank1", "Alice at Meta", retained_at=_future(2)),
        ])

        # as_of = now → nothing visible
        result = await brain.history("Alice", bank_id="bank1", as_of=datetime.now(UTC))
        assert result.hits == []

    @pytest.mark.asyncio
    async def test_history_respects_max_results(self):
        brain, vs = self._brain()

        for i in range(5):
            await vs.store_vectors([
                _item(f"m{i}", "bank1", f"Alice memory {i}", retained_at=_ts(i + 1))
            ])

        result = await brain.history("Alice memory", bank_id="bank1", as_of=datetime.now(UTC), max_results=2)
        assert len(result.hits) <= 2

    @pytest.mark.asyncio
    async def test_history_as_of_in_result(self):
        brain, vs = self._brain()
        ts = _ts(5)

        result = await brain.history("query", bank_id="bank1", as_of=ts)
        assert result.as_of == ts

    @pytest.mark.asyncio
    async def test_brain_retain_then_history(self):
        """End-to-end: retain via brain, then history at a checkpoint."""
        brain, _ = self._brain()

        checkpoint = datetime.now(UTC)

        # Retain after checkpoint
        await brain.retain("Alice now works at Google.", bank_id="bank1")

        # History as_of checkpoint → retained_at is after checkpoint, so excluded
        result = await brain.history("Alice", bank_id="bank1", as_of=checkpoint)
        # Memory was retained AFTER checkpoint, so should not appear
        assert not any("Google" in h.text for h in result.hits)
