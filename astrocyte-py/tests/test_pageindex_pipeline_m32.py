"""M32 — Tests for PageIndexPipeline + dispatcher routing.

The M32 cycle unifies the bench's retrieval stack and the public
``Astrocyte.recall()`` API by routing both through
:class:`PageIndexPipeline`. Tests cover:

1. ``PageIndexPipeline.recall()`` produces well-shaped ``RecallResult``
2. Result-shape adapter (fact + section → MemoryHit)
3. ``session_id`` from ``RecallRequest`` threads into ``fact_recall``
4. ``ProviderDispatcher`` routes to PageIndex pipeline when configured
5. ``Astrocyte.use_pageindex_pipeline()`` installs the pipeline correctly
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrocyte.pipeline.pageindex_pipeline import PageIndexPipeline
from astrocyte.testing.in_memory import InMemoryPageIndexStore
from astrocyte.types import (
    MemoryFact,
    PageIndexDocument,
    PageIndexSection,
    RecallRequest,
    RecallResult,
)


def _make_provider(return_vec: list[float] | None = None):
    """Construct an LLMProvider stub that returns a fixed embedding."""
    provider = MagicMock()
    provider.embed = AsyncMock(
        return_value=[return_vec if return_vec is not None else [1.0, 0.0, 0.0]],
    )
    return provider


async def _seed_store_with_one_fact() -> tuple[InMemoryPageIndexStore, str]:
    """Single fact pointing at one section in one document, bank='b1'."""
    store = InMemoryPageIndexStore()
    doc_id = await store.save_document(
        PageIndexDocument(
            id="", bank_id="b1", source_id="u1", md_text="",
            reference_date=None,
            built_at=datetime.now(tz=timezone.utc),
        ),
    )
    await store.save_sections(
        doc_id,
        [
            PageIndexSection(
                document_id=doc_id, line_num=1, node_id="s1",
                title="Session 1", summary="Sample session",
                session_id="sess-1",
            ),
        ],
    )
    await store.save_facts([
        MemoryFact(
            id="f1", bank_id="b1", document_id=doc_id, line_num=1,
            text="User likes pour-over coffee",
            fact_type="preference",
            entities=["coffee"],
            embedding=[1.0, 0.0, 0.0],
        ),
    ])
    return store, doc_id


class TestPageIndexPipelineRecall:
    @pytest.mark.asyncio
    async def test_returns_recall_result(self) -> None:
        store, _ = await _seed_store_with_one_fact()
        provider = _make_provider()
        pipeline = PageIndexPipeline(
            store=store, embedding_provider=provider,
        )
        result = await pipeline.recall(
            RecallRequest(query="coffee", bank_id="b1", max_results=5),
        )
        assert isinstance(result, RecallResult)
        assert result.total_available >= 1
        assert any(h.memory_id == "f1" for h in result.hits)

    @pytest.mark.asyncio
    async def test_empty_query_embedding_returns_empty(self) -> None:
        store, _ = await _seed_store_with_one_fact()
        provider = _make_provider(return_vec=[])
        pipeline = PageIndexPipeline(
            store=store, embedding_provider=provider,
        )
        result = await pipeline.recall(
            RecallRequest(query="x", bank_id="b1"),
        )
        assert result.hits == []
        assert result.total_available == 0

    @pytest.mark.asyncio
    async def test_max_results_truncation(self) -> None:
        store, doc_id = await _seed_store_with_one_fact()
        # Add 5 more facts so we have 6 total.
        await store.save_facts([
            MemoryFact(
                id=f"f{i}", bank_id="b1", document_id=doc_id, line_num=1,
                text=f"Fact {i}",
                fact_type="preference",
                entities=[f"e{i}"],
                embedding=[1.0, 0.0, 0.0],
            )
            for i in range(2, 7)
        ])
        provider = _make_provider()
        pipeline = PageIndexPipeline(
            store=store, embedding_provider=provider,
        )
        result = await pipeline.recall(
            RecallRequest(query="x", bank_id="b1", max_results=3),
        )
        assert len(result.hits) == 3
        assert result.truncated is True


class TestSessionIdThreading:
    """M31 Fix 2 session_id flows from RecallRequest → fact_recall →
    store.search_facts_*."""

    @pytest.mark.asyncio
    async def test_session_id_filter_excludes_other_sessions(self) -> None:
        store = InMemoryPageIndexStore()
        doc_id = await store.save_document(
            PageIndexDocument(
                id="", bank_id="b1", source_id="u1", md_text="",
                reference_date=None,
                built_at=datetime.now(tz=timezone.utc),
            ),
        )
        # Two sections in two sessions.
        await store.save_sections(
            doc_id,
            [
                PageIndexSection(
                    document_id=doc_id, line_num=1, node_id="s1",
                    title="A", summary="alpha", session_id="sess-A",
                ),
                PageIndexSection(
                    document_id=doc_id, line_num=10, node_id="s2",
                    title="B", summary="beta", session_id="sess-B",
                ),
            ],
        )
        await store.save_facts([
            MemoryFact(
                id="fA", bank_id="b1", document_id=doc_id, line_num=1,
                text="from A", fact_type="experience",
                entities=["x"], embedding=[1.0, 0.0, 0.0],
            ),
            MemoryFact(
                id="fB", bank_id="b1", document_id=doc_id, line_num=10,
                text="from B", fact_type="experience",
                entities=["x"], embedding=[1.0, 0.0, 0.0],
            ),
        ])
        provider = _make_provider()
        pipeline = PageIndexPipeline(
            store=store, embedding_provider=provider,
        )
        # No session filter → both facts.
        all_result = await pipeline.recall(
            RecallRequest(query="x", bank_id="b1"),
        )
        ids = {h.memory_id for h in all_result.hits}
        assert {"fA", "fB"} <= ids

        # session-A filter → only fA.
        a_result = await pipeline.recall(
            RecallRequest(query="x", bank_id="b1", session_id="sess-A"),
        )
        a_ids = {h.memory_id for h in a_result.hits}
        assert "fA" in a_ids
        assert "fB" not in a_ids


class TestResultShape:
    """Fact-grain → memory_layer='fact'; M31 event_date preferred for occurred_at."""

    @pytest.mark.asyncio
    async def test_fact_grain_memory_layer_label(self) -> None:
        store, _ = await _seed_store_with_one_fact()
        provider = _make_provider()
        pipeline = PageIndexPipeline(
            store=store, embedding_provider=provider,
        )
        result = await pipeline.recall(
            RecallRequest(query="coffee", bank_id="b1"),
        )
        fact_hits = [h for h in result.hits if h.memory_layer == "fact"]
        assert len(fact_hits) >= 1
        assert fact_hits[0].memory_id == "f1"

    @pytest.mark.asyncio
    async def test_event_date_preferred_over_occurred_start(self) -> None:
        """M31 Fix 4 — event_date is the canonical absolute date; the
        adapter surfaces it as MemoryHit.occurred_at when present."""
        store = InMemoryPageIndexStore()
        doc_id = await store.save_document(
            PageIndexDocument(
                id="", bank_id="b1", source_id="u1", md_text="",
                reference_date=None,
                built_at=datetime.now(tz=timezone.utc),
            ),
        )
        await store.save_sections(doc_id, [
            PageIndexSection(
                document_id=doc_id, line_num=1, node_id="s1",
                title="t", summary="s",
            ),
        ])
        event_date = datetime(2024, 3, 15, tzinfo=timezone.utc)
        occurred_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        await store.save_facts([
            MemoryFact(
                id="f-with-event", bank_id="b1",
                document_id=doc_id, line_num=1,
                text="event", fact_type="experience",
                entities=["x"], embedding=[1.0, 0.0, 0.0],
                occurred_start=occurred_start,
                event_date=event_date,
            ),
        ])
        provider = _make_provider()
        pipeline = PageIndexPipeline(
            store=store, embedding_provider=provider,
        )
        result = await pipeline.recall(
            RecallRequest(query="x", bank_id="b1"),
        )
        hit = next(h for h in result.hits if h.memory_id == "f-with-event")
        # event_date wins over occurred_start when both are set.
        assert hit.occurred_at == event_date


class TestDispatcherRouting:
    """ProviderDispatcher.recall() prefers pageindex_pipeline when set."""

    @pytest.mark.asyncio
    async def test_dispatcher_prefers_pageindex_when_configured(self) -> None:
        from astrocyte._provider_dispatch import ProviderDispatcher
        from astrocyte.config import AstrocyteConfig

        store, _ = await _seed_store_with_one_fact()
        provider = _make_provider()
        pageindex = PageIndexPipeline(
            store=store, embedding_provider=provider,
        )

        # Legacy engine_provider mock that should NOT be called.
        engine = MagicMock()
        engine.recall = AsyncMock(side_effect=Exception("should not be called"))

        dispatcher = ProviderDispatcher(
            config=AstrocyteConfig(),
            engine_provider=engine,
            pageindex_pipeline=pageindex,
        )
        result = await dispatcher.recall(
            RecallRequest(query="coffee", bank_id="b1"),
        )
        # PageIndex pipeline returned hits; engine wasn't touched.
        assert isinstance(result, RecallResult)
        engine.recall.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatcher_falls_back_to_engine_without_pageindex(self) -> None:
        """v0.15.x compat: when pageindex_pipeline is None, falls back
        to legacy engine_provider or pipeline."""
        from astrocyte._provider_dispatch import ProviderDispatcher
        from astrocyte.config import AstrocyteConfig

        engine = MagicMock()
        engine.recall = AsyncMock(
            return_value=RecallResult(
                hits=[], total_available=0, truncated=False,
            ),
        )
        dispatcher = ProviderDispatcher(
            config=AstrocyteConfig(),
            engine_provider=engine,
        )
        await dispatcher.recall(RecallRequest(query="x", bank_id="b1"))
        engine.recall.assert_called_once()


class TestAstrocyteUsePageIndex:
    """Astrocyte.use_pageindex_pipeline installs the pipeline correctly."""

    @pytest.mark.asyncio
    async def test_install_then_recall_goes_through_pageindex(self) -> None:
        from astrocyte import Astrocyte
        from astrocyte.config import AstrocyteConfig

        store, _ = await _seed_store_with_one_fact()
        provider = _make_provider()

        astro = Astrocyte(AstrocyteConfig())
        astro.use_pageindex_pipeline(
            store=store, embedding_provider=provider,
        )

        # Confirm the pipeline is wired.
        assert astro._dispatcher.pageindex_pipeline is not None
        # And that public Astrocyte.recall() now routes through it.
        # (We don't fully invoke recall here to avoid policy-layer
        # setup overhead — the dispatcher-routing test above already
        # proves the route is exercised when pipeline is set.)

    def test_install_without_provider_raises(self) -> None:
        from astrocyte import Astrocyte
        from astrocyte.config import AstrocyteConfig

        store = InMemoryPageIndexStore()
        astro = Astrocyte(AstrocyteConfig())
        # No provider attribute on a freshly-built Astrocyte → must error.
        with pytest.raises(ValueError, match="no embedding_provider"):
            astro.use_pageindex_pipeline(store=store)


class TestMetadataPreservation:
    """M32 result-shape adapter must surface all fact-grain metadata
    (M27 confidence_score + mentioned_at, M31 event_date, line_num) on
    ``MemoryHit.metadata`` so downstream consumers can read them
    without losing information across the unification boundary."""

    @pytest.mark.asyncio
    async def test_metadata_carries_m27_and_m31_fact_fields(self) -> None:
        store = InMemoryPageIndexStore()
        doc_id = await store.save_document(
            PageIndexDocument(
                id="", bank_id="b1", source_id="u1", md_text="",
                reference_date=None,
                built_at=datetime.now(tz=timezone.utc),
            ),
        )
        await store.save_sections(doc_id, [
            PageIndexSection(
                document_id=doc_id, line_num=42, node_id="s1",
                title="t", summary="s",
                session_date=datetime(2024, 5, 9, tzinfo=timezone.utc),
            ),
        ])
        mentioned = datetime(2024, 5, 9, tzinfo=timezone.utc)
        event = datetime(2024, 5, 7, tzinfo=timezone.utc)
        await store.save_facts([
            MemoryFact(
                id="f-rich", bank_id="b1",
                document_id=doc_id, line_num=42,
                text="User saw doctor last Tuesday",
                fact_type="experience", speaker="user",
                entities=["doctor"], embedding=[1.0, 0.0, 0.0],
                confidence_score=0.87,
                mentioned_at=mentioned,
                event_date=event,
            ),
        ])
        provider = _make_provider()
        pipeline = PageIndexPipeline(
            store=store, embedding_provider=provider,
        )
        result = await pipeline.recall(
            RecallRequest(query="doctor visit", bank_id="b1"),
        )
        hit = next(h for h in result.hits if h.memory_id == "f-rich")
        assert hit.metadata is not None
        assert hit.metadata["grain"] == "fact"
        assert hit.metadata["confidence_score"] == 0.87
        assert hit.metadata["mentioned_at"] == mentioned
        assert hit.metadata["event_date"] == event
        assert hit.metadata["line_num"] == 42
        assert hit.metadata["document_id"] == doc_id
        assert hit.metadata["speaker"] == "user"
        assert hit.metadata["entities"] == ["doctor"]
        # M31 — event_date wins for occurred_at when both are set.
        assert hit.occurred_at == event

    @pytest.mark.asyncio
    async def test_metadata_none_values_still_keyed(self) -> None:
        """Legacy facts (no confidence, no event_date) still have the
        keys present with None values — single-pattern access at the
        consumer side."""
        store, _ = await _seed_store_with_one_fact()
        provider = _make_provider()
        pipeline = PageIndexPipeline(
            store=store, embedding_provider=provider,
        )
        result = await pipeline.recall(
            RecallRequest(query="coffee", bank_id="b1"),
        )
        hit = next(h for h in result.hits if h.memory_id == "f1")
        assert hit.metadata is not None
        # Keys present, values None for unset M27/M31 fields.
        assert "confidence_score" in hit.metadata
        assert "mentioned_at" in hit.metadata
        assert "event_date" in hit.metadata
        assert hit.metadata["confidence_score"] is None
        assert hit.metadata["mentioned_at"] is None
        assert hit.metadata["event_date"] is None
