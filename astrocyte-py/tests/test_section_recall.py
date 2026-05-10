"""Tests for ``astrocyte.pipeline.section_recall``.

Three layers:

1. **Pure helpers** — ``_rrf_fuse_section_hits`` and
   ``select_strategies_for_mode`` are pure functions; tests pin RRF
   correctness and the per-mode strategy mix.

2. **Strategy dispatch** — each mode (default / temporal / multi-hop /
   assistant-recall) runs the right subset of strategies. Verified by
   inspecting the returned ``SectionRecallResult.strategies`` list.

3. **Failure isolation** — a strategy that raises must not crash the
   call. The orchestrator captures the exception in
   ``StrategyResult.error`` and the other strategies still contribute
   to fusion. Verified with a fault-injection store.

Tests run against ``InMemoryPageIndexStore`` so they're fast and
dep-free.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from astrocyte.pipeline.section_recall import (
    FusedHit,
    SectionRecallResult,
    StrategyResult,
    _rrf_fuse_section_hits,
    section_recall,
    select_strategies_for_mode,
)
from astrocyte.testing.in_memory import (
    InMemoryPageIndexStore,
    MockLLMProvider,
)
from astrocyte.types import (
    PageIndexDocument,
    PageIndexSection,
    PageIndexSectionEntity,
    PageIndexSectionLink,
)

# ---------------------------------------------------------------------------
# _rrf_fuse_section_hits — RRF over (doc, line, score) tuples
# ---------------------------------------------------------------------------


class TestRRFFuseSectionHits:
    def test_single_strategy_preserves_order(self):
        sr = StrategyResult(
            strategy="semantic",
            hits=[("d1", 1, 0.9), ("d1", 2, 0.5), ("d1", 3, 0.1)],
            elapsed_ms=1.0,
        )
        fused = _rrf_fuse_section_hits([sr], k=60)

        assert [(h.document_id, h.line_num) for h in fused] == [
            ("d1", 1), ("d1", 2), ("d1", 3),
        ]
        # rrf_score = 1/(k+rank), monotonically decreasing.
        assert fused[0].rrf_score > fused[1].rrf_score > fused[2].rrf_score
        assert fused[0].per_strategy_rank == {"semantic": 1}

    def test_two_strategies_combine_scores(self):
        sem = StrategyResult(
            strategy="semantic",
            hits=[("d1", 1, 0.9), ("d1", 2, 0.5)],
            elapsed_ms=1.0,
        )
        kw = StrategyResult(
            strategy="keyword",
            hits=[("d1", 2, 0.8), ("d1", 3, 0.3)],
            elapsed_ms=1.0,
        )
        fused = _rrf_fuse_section_hits([sem, kw], k=60)

        # Section 2 appears in both → highest score; sections 1 & 3 each appear once.
        first = fused[0]
        assert (first.document_id, first.line_num) == ("d1", 2)
        assert first.per_strategy_rank == {"semantic": 2, "keyword": 1}
        # Score = 1/(k+rank_semantic) + 1/(k+rank_keyword)
        #       = 1/(60+2)         + 1/(60+1)
        #       = 1/62             + 1/61
        assert first.rrf_score == pytest.approx(1.0 / 62.0 + 1.0 / 61.0)

    def test_strategy_with_error_is_skipped(self):
        good = StrategyResult(
            strategy="semantic",
            hits=[("d1", 1, 0.9)],
            elapsed_ms=1.0,
        )
        broken = StrategyResult(
            strategy="keyword",
            hits=[("d1", 2, 0.8)],  # nominal hits, but error set
            elapsed_ms=1.0,
            error="RuntimeError: synthetic",
        )
        fused = _rrf_fuse_section_hits([good, broken], k=60)

        # Only the semantic hit makes it through; keyword's hits are
        # ignored because error is set.
        assert len(fused) == 1
        assert (fused[0].document_id, fused[0].line_num) == ("d1", 1)
        assert "keyword" not in fused[0].per_strategy_rank

    def test_empty_input_returns_empty(self):
        assert _rrf_fuse_section_hits([]) == []

    def test_all_strategies_empty_returns_empty(self):
        sr = StrategyResult(strategy="semantic", hits=[], elapsed_ms=0.0)
        assert _rrf_fuse_section_hits([sr]) == []


# ---------------------------------------------------------------------------
# select_strategies_for_mode — per-mode strategy mix
# ---------------------------------------------------------------------------


class TestSelectStrategiesForMode:
    def test_default_modes_return_baseline(self):
        baseline = {"semantic", "keyword", "entity"}
        for mode in ("default", "single-session", "single-session-user", "open-domain"):
            assert select_strategies_for_mode(mode) == baseline, mode

    @pytest.mark.parametrize("mode", ["temporal", "temporal-reasoning"])
    def test_temporal_modes_add_temporal_strategy(self, mode):
        assert select_strategies_for_mode(mode) == {
            "semantic", "keyword", "entity", "temporal",
        }

    @pytest.mark.parametrize(
        "mode",
        ["multi-hop", "multi-session", "knowledge-update"],
    )
    def test_multi_hop_modes_add_graph_expand(self, mode):
        assert select_strategies_for_mode(mode) == {
            "semantic", "keyword", "entity", "graph_expand",
        }

    @pytest.mark.parametrize(
        "mode",
        ["single-session-assistant", "assistant-recall"],
    )
    def test_assistant_modes_use_baseline_set(self, mode):
        """The speaker filter is applied inline by the orchestrator on
        the keyword strategy — the strategy SET is the baseline."""
        assert select_strategies_for_mode(mode) == {
            "semantic", "keyword", "entity",
        }


# ---------------------------------------------------------------------------
# section_recall — end-to-end against InMemoryPageIndexStore
# ---------------------------------------------------------------------------


@pytest.fixture
async def populated_store():
    """Doc with three sections + entity rows + temporal stamps + a
    section_link for the graph-expand path."""
    store = InMemoryPageIndexStore()
    doc = PageIndexDocument(
        id="",
        bank_id="b1",
        source_id="conv-1",
        md_text="line 1\nline 2\nline 3\n",
        reference_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    doc_id = await store.save_document(doc)

    sections = [
        PageIndexSection(
            document_id=doc_id,
            line_num=ln,
            node_id=f"{ln:04d}",
            title=f"Section {ln} about Alice",
            summary=f"Alice did thing {ln}",
            speaker="user" if ln != 2 else "assistant",
            session_date=datetime(2026, 3, ln, tzinfo=timezone.utc),
            depth=1,
        )
        for ln in (1, 2, 3)
    ]
    await store.save_sections(doc_id, sections)

    # Entity rows so the entity strategy has something to return.
    await store.save_section_entities([
        PageIndexSectionEntity(document_id=doc_id, line_num=1, entity_name="Alice"),
        PageIndexSectionEntity(document_id=doc_id, line_num=2, entity_name="Alice"),
    ])

    # Section link 1 → 3 (graph-expand path).
    await store.save_section_links([
        PageIndexSectionLink(
            from_doc=doc_id, from_line=1,
            to_doc=doc_id, to_line=3,
            link_type="semantic_knn", weight=0.9,
        ),
    ])
    return store, doc_id


class TestSectionRecallDispatch:
    """The right strategies fire for each mode."""

    @pytest.mark.asyncio
    async def test_default_mode_runs_baseline_three(self, populated_store):
        store, _ = populated_store
        result = await section_recall(
            store=store,
            bank_id="b1",
            question="alice",
            mode="default",
            embedding_provider=MockLLMProvider(),
            question_entities=["Alice"],  # for entity strategy to run
        )
        names = {s.strategy for s in result.strategies}
        assert names == {"semantic", "keyword", "entity"}

    @pytest.mark.asyncio
    async def test_temporal_mode_adds_temporal_strategy(self, populated_store):
        store, _ = populated_store
        result = await section_recall(
            store=store,
            bank_id="b1",
            question="what happened in March 2026?",
            mode="temporal",
            embedding_provider=MockLLMProvider(),
            question_entities=["Alice"],
            date_range=(
                datetime(2026, 3, 1, tzinfo=timezone.utc),
                datetime(2026, 3, 31, tzinfo=timezone.utc),
            ),
        )
        names = {s.strategy for s in result.strategies}
        assert "temporal" in names

    @pytest.mark.asyncio
    async def test_multi_hop_mode_adds_graph_expand(self, populated_store):
        store, doc_id = populated_store
        result = await section_recall(
            store=store,
            bank_id="b1",
            question="alice",
            mode="multi-hop",
            embedding_provider=MockLLMProvider(),
            question_entities=["Alice"],
        )
        names = {s.strategy for s in result.strategies}
        assert "graph_expand" in names
        # The link 1→3 should surface line 3 via expansion (line 1
        # appears as a semantic/entity seed; expansion fans out to 3).
        graph = next(s for s in result.strategies if s.strategy == "graph_expand")
        graph_lines = {ln for _doc, ln, _ in graph.hits}
        assert 3 in graph_lines

    @pytest.mark.asyncio
    async def test_assistant_recall_filters_keyword_to_assistant(self, populated_store):
        """In assistant-recall mode the orchestrator passes
        ``speaker='assistant'`` to the keyword strategy. The
        InMemoryPageIndexStore implements that filter, so only line 2
        (the assistant-spoken section in the fixture) can appear in
        the keyword hits."""
        store, _ = populated_store
        result = await section_recall(
            store=store,
            bank_id="b1",
            question="alice",
            mode="assistant-recall",
            embedding_provider=MockLLMProvider(),
        )
        kw = next(s for s in result.strategies if s.strategy == "keyword")
        kw_lines = {ln for _doc, ln, _ in kw.hits}
        assert kw_lines.issubset({2}), (
            f"assistant-mode keyword should only return assistant-spoken "
            f"sections; got lines {kw_lines}"
        )


class TestSectionRecallSkipsWhenInputsMissing:
    """The entity / temporal strategies are no-ops when the caller
    didn't pre-extract entities / a date_range (because the question
    annotator hasn't run, or the question simply didn't mention dates)."""

    @pytest.mark.asyncio
    async def test_no_question_entities_skips_entity_strategy(self, populated_store):
        store, _ = populated_store
        result = await section_recall(
            store=store,
            bank_id="b1",
            question="alice",
            mode="default",
            embedding_provider=MockLLMProvider(),
            question_entities=None,  # explicit skip
        )
        entity_sr = next(s for s in result.strategies if s.strategy == "entity")
        assert entity_sr.hits == []
        assert entity_sr.elapsed_ms == 0.0  # never reached the store
        assert entity_sr.error is None

    @pytest.mark.asyncio
    async def test_no_date_range_skips_temporal_strategy(self, populated_store):
        store, _ = populated_store
        result = await section_recall(
            store=store,
            bank_id="b1",
            question="anything",
            mode="temporal",
            embedding_provider=MockLLMProvider(),
            date_range=None,  # explicit skip even though mode requested temporal
        )
        temp_sr = next(s for s in result.strategies if s.strategy == "temporal")
        assert temp_sr.hits == []
        assert temp_sr.elapsed_ms == 0.0


class TestSectionRecallFailureIsolation:
    """A strategy that raises is captured into StrategyResult.error;
    other strategies and the fused output keep working."""

    @pytest.mark.asyncio
    async def test_semantic_failure_doesnt_crash_call(self, populated_store):
        store, _ = populated_store

        class FailingSemanticStore:
            """Wraps the populated store; raises only on semantic search."""
            def __init__(self, inner):
                self._inner = inner
            async def search_sections_semantic(self, *a, **kw):
                raise RuntimeError("synthetic semantic failure")
            # delegate the rest
            async def search_sections_keyword(self, *a, **kw):
                return await self._inner.search_sections_keyword(*a, **kw)
            async def search_sections_by_entities(self, *a, **kw):
                return await self._inner.search_sections_by_entities(*a, **kw)
            async def search_sections_temporal(self, *a, **kw):
                return await self._inner.search_sections_temporal(*a, **kw)
            async def expand_section_links(self, *a, **kw):
                return await self._inner.expand_section_links(*a, **kw)

        result = await section_recall(
            store=FailingSemanticStore(store),  # type: ignore[arg-type]
            bank_id="b1",
            question="alice",
            mode="default",
            embedding_provider=MockLLMProvider(),
            question_entities=["Alice"],
        )
        # Semantic strategy errored; the result type captures it.
        sem = next(s for s in result.strategies if s.strategy == "semantic")
        assert sem.error is not None
        assert "synthetic semantic failure" in sem.error
        # Other strategies still ran and produced hits.
        kw = next(s for s in result.strategies if s.strategy == "keyword")
        assert kw.error is None
        # Fusion still produced output (from keyword + entity).
        assert isinstance(result, SectionRecallResult)

    @pytest.mark.asyncio
    async def test_entity_strategy_failure_captured(self, populated_store):
        """``search_sections_by_entities`` raising must not crash the call —
        the error lands in StrategyResult.error and other strategies still run."""
        store, _ = populated_store

        class FailingEntityStore:
            def __init__(self, inner):
                self._inner = inner
            async def search_sections_semantic(self, *a, **kw):
                return await self._inner.search_sections_semantic(*a, **kw)
            async def search_sections_keyword(self, *a, **kw):
                return await self._inner.search_sections_keyword(*a, **kw)
            async def search_sections_by_entities(self, *a, **kw):
                raise RuntimeError("synthetic entity failure")
            async def search_sections_temporal(self, *a, **kw):
                return await self._inner.search_sections_temporal(*a, **kw)
            async def expand_section_links(self, *a, **kw):
                return await self._inner.expand_section_links(*a, **kw)

        result = await section_recall(
            store=FailingEntityStore(store),  # type: ignore[arg-type]
            bank_id="b1",
            question="alice",
            mode="default",
            embedding_provider=MockLLMProvider(),
            question_entities=["Alice"],  # forces the entity strategy to actually run
        )
        ent = next(s for s in result.strategies if s.strategy == "entity")
        assert ent.error is not None
        assert "synthetic entity failure" in ent.error
        assert ent.hits == []

    @pytest.mark.asyncio
    async def test_temporal_strategy_failure_captured(self, populated_store):
        """``search_sections_temporal`` raising must not crash the call."""
        store, _ = populated_store

        class FailingTemporalStore:
            def __init__(self, inner):
                self._inner = inner
            async def search_sections_semantic(self, *a, **kw):
                return await self._inner.search_sections_semantic(*a, **kw)
            async def search_sections_keyword(self, *a, **kw):
                return await self._inner.search_sections_keyword(*a, **kw)
            async def search_sections_by_entities(self, *a, **kw):
                return await self._inner.search_sections_by_entities(*a, **kw)
            async def search_sections_temporal(self, *a, **kw):
                raise RuntimeError("synthetic temporal failure")
            async def expand_section_links(self, *a, **kw):
                return await self._inner.expand_section_links(*a, **kw)

        result = await section_recall(
            store=FailingTemporalStore(store),  # type: ignore[arg-type]
            bank_id="b1",
            question="anything",
            mode="temporal",
            embedding_provider=MockLLMProvider(),
            date_range=(
                datetime(2026, 3, 1, tzinfo=timezone.utc),
                datetime(2026, 3, 31, tzinfo=timezone.utc),
            ),
        )
        temp = next(s for s in result.strategies if s.strategy == "temporal")
        assert temp.error is not None
        assert "synthetic temporal failure" in temp.error
        assert temp.hits == []

    @pytest.mark.asyncio
    async def test_graph_expand_failure_captured(self, populated_store):
        """Graph-expand runs on the second pass (after semantic + entity
        seeds resolve). A failure there must not crash the call."""
        store, _ = populated_store

        class FailingExpandStore:
            def __init__(self, inner):
                self._inner = inner
            async def search_sections_semantic(self, *a, **kw):
                return await self._inner.search_sections_semantic(*a, **kw)
            async def search_sections_keyword(self, *a, **kw):
                return await self._inner.search_sections_keyword(*a, **kw)
            async def search_sections_by_entities(self, *a, **kw):
                return await self._inner.search_sections_by_entities(*a, **kw)
            async def search_sections_temporal(self, *a, **kw):
                return await self._inner.search_sections_temporal(*a, **kw)
            async def expand_section_links(self, *a, **kw):
                raise RuntimeError("synthetic graph-expand failure")

        result = await section_recall(
            store=FailingExpandStore(store),  # type: ignore[arg-type]
            bank_id="b1",
            question="alice",
            mode="multi-hop",
            embedding_provider=MockLLMProvider(),
            question_entities=["Alice"],
        )
        graph = next(s for s in result.strategies if s.strategy == "graph_expand")
        assert graph.error is not None
        assert "synthetic graph-expand failure" in graph.error
        assert graph.hits == []


class TestSectionRecallResultShape:
    @pytest.mark.asyncio
    async def test_carries_mode_and_elapsed_ms(self, populated_store):
        store, _ = populated_store
        result = await section_recall(
            store=store,
            bank_id="b1",
            question="alice",
            mode="default",
            embedding_provider=MockLLMProvider(),
        )
        assert result.mode == "default"
        assert result.elapsed_ms >= 0.0
        # Each strategy carries its own timing.
        for s in result.strategies:
            assert isinstance(s, StrategyResult)
            assert s.elapsed_ms >= 0.0

    @pytest.mark.asyncio
    async def test_fused_hits_are_sorted_descending(self, populated_store):
        store, _ = populated_store
        result = await section_recall(
            store=store,
            bank_id="b1",
            question="alice",
            mode="default",
            embedding_provider=MockLLMProvider(),
            question_entities=["Alice"],
        )
        scores = [h.rrf_score for h in result.fused]
        assert scores == sorted(scores, reverse=True)
        for h in result.fused:
            assert isinstance(h, FusedHit)
