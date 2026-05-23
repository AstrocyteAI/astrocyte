"""Tests for `astrocyte.pipeline.fact_recall.fact_recall`.

Verifies (RRF-fused architecture):
  - Semantic search always runs
  - Episodic search runs only when (a) config flag on AND (b) question has cue
  - Temporal search runs only when caller passes ``temporal_range``
  - Failures in any branch are isolated (other branches still produce)
  - Dedupe by fact_id with RRF score accumulation
  - RRF ordering: cross-strategy agreement ranks above single-strategy hits
"""

from __future__ import annotations

from datetime import datetime

import pytest

from astrocyte.config import AstrocyteConfig, EpisodicExtractConfig
from astrocyte.pipeline.fact_recall import fact_recall


def _cfg(episodic: bool = False) -> AstrocyteConfig:
    c = AstrocyteConfig()
    c.episodic_extract = EpisodicExtractConfig(enabled=episodic)
    return c


class _FactHit:
    """Minimal stand-in for a PageIndexFact hit."""

    def __init__(self, fact_id: str, text: str = "", fact_type: str | None = None):
        self.fact_id = fact_id
        self.text = text
        self.fact_type = fact_type


class _FakeStore:
    def __init__(
        self,
        semantic_hits=None,
        episodic_hits=None,
        temporal_hits=None,
        link_hits_by_entity=None,
        keyword_hits=None,  # M31c
        fail_semantic: bool = False,
        fail_episodic: bool = False,
        fail_temporal: bool = False,
        fail_keyword: bool = False,  # M31c
    ):
        self._semantic_hits = semantic_hits or []
        self._episodic_hits = episodic_hits or []
        self._temporal_hits = temporal_hits or []
        # M27 — link-expansion: per-entity hit lists; keys are
        # entity names, values are list[_FactHit] returned when that
        # entity is queried. Episodic uses EPISODIC_MARKER as the key.
        self._link_hits_by_entity: dict = link_hits_by_entity or {}
        self._keyword_hits = keyword_hits or []  # M31c
        self._fail_semantic = fail_semantic
        self._fail_episodic = fail_episodic
        self._fail_temporal = fail_temporal
        self._fail_keyword = fail_keyword  # M31c
        self.semantic_calls = 0
        self.episodic_calls = 0
        self.temporal_calls = 0
        self.link_expansion_calls = 0
        self.keyword_calls = 0  # M31c
        # Track which entities were queried (link-expansion observability).
        self.link_expansion_entities: list[str] = []

    async def search_facts_semantic(
        self, bank_id, qvec, *, top_k, document_id, fact_type=None, session_filter=None,
    ):
        self.semantic_calls += 1
        # Track session_filter so M31 Fix 2 tests can assert it threaded through.
        self.last_session_filter_semantic = session_filter
        self.last_fact_type_semantic = fact_type  # M34-3
        if self._fail_semantic:
            raise RuntimeError("semantic broke")
        # M34-4 — when fact_type is set, filter hits to only those whose
        # fact_type attribute matches. Hits w/o fact_type default to None
        # (don't match any specific filter).
        if fact_type is not None:
            return [
                h for h in self._semantic_hits
                if getattr(h, "fact_type", None) == fact_type
            ]
        return self._semantic_hits

    async def search_facts_by_entity(
        self, bank_id, entity, *, top_k, document_id, fact_type=None, session_filter=None,
    ):
        # Disambiguate: episodic uses EPISODIC_MARKER (a single sentinel
        # entity); link-expansion passes real query entities AND uses
        # document_id=None. Both paths go through this method.
        from astrocyte.pipeline.episodic_extract import EPISODIC_MARKER

        if entity == EPISODIC_MARKER:
            self.episodic_calls += 1
            self.last_session_filter_episodic = session_filter
            self.last_fact_type_episodic = fact_type  # M34-3
            if self._fail_episodic:
                raise RuntimeError("episodic broke")
            hits = self._episodic_hits
            if fact_type is not None:
                hits = [h for h in hits if getattr(h, "fact_type", None) == fact_type]
            return hits
        # Link-expansion path — non-episodic entity, typically with
        # document_id=None (cross-session). M31 Fix 2: link-expansion
        # MUST receive session_filter=None even when caller set one,
        # because the strategy's whole point is cross-session traversal.
        self.link_expansion_calls += 1
        self.link_expansion_entities.append(entity)
        self.last_session_filter_link = session_filter
        self.last_fact_type_link = fact_type  # M34-3
        hits = self._link_hits_by_entity.get(entity, [])
        if fact_type is not None:
            hits = [h for h in hits if getattr(h, "fact_type", None) == fact_type]
        return hits

    async def search_facts_temporal(
        self, bank_id, date_range, *, top_k, document_id, fact_type=None, session_filter=None,
    ):
        self.temporal_calls += 1
        self.last_session_filter_temporal = session_filter
        self.last_fact_type_temporal = fact_type  # M34-3
        if self._fail_temporal:
            raise RuntimeError("temporal broke")
        hits = self._temporal_hits
        if fact_type is not None:
            hits = [h for h in hits if getattr(h, "fact_type", None) == fact_type]
        return hits

    async def search_facts_keyword(
        self, bank_id, query, *, top_k, document_id, fact_type=None, session_filter=None,
    ):
        # M31c — track invocation so RRF-threading tests can assert
        # the new branch fires.
        self.keyword_calls = getattr(self, "keyword_calls", 0) + 1
        self.last_session_filter_keyword = session_filter
        self.last_keyword_query = query
        self.last_fact_type_keyword = fact_type  # M34-3
        if getattr(self, "_fail_keyword", False):
            raise RuntimeError("keyword broke")
        hits = getattr(self, "_keyword_hits", [])
        if fact_type is not None:
            hits = [h for h in hits if getattr(h, "fact_type", None) == fact_type]
        return hits


# ────────────────────────────────────────────────────────────────────────
# Branch gating
# ────────────────────────────────────────────────────────────────────────


class TestSemanticAlwaysRuns:
    @pytest.mark.asyncio
    async def test_returns_semantic_when_all_other_branches_off(self) -> None:
        store = _FakeStore(semantic_hits=[_FactHit("f1"), _FactHit("f2")])
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="who attended the wedding?",
            query_embedding=[0.1, 0.2], config=_cfg(episodic=False),
        )
        assert [h.fact_id for h in out] == ["f1", "f2"]
        assert store.semantic_calls == 1
        assert store.episodic_calls == 0
        assert store.temporal_calls == 0


class TestEpisodicGating:
    @pytest.mark.asyncio
    async def test_episodic_runs_when_flag_on_and_cue_present(self) -> None:
        store = _FakeStore(
            semantic_hits=[_FactHit("f1")],
            episodic_hits=[_FactHit("e1"), _FactHit("e2")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="where did I meet Alice?",
            query_embedding=[0.1], config=_cfg(episodic=True),
        )
        ids = [h.fact_id for h in out]
        assert "f1" in ids
        assert "e1" in ids and "e2" in ids
        assert store.episodic_calls == 1

    @pytest.mark.asyncio
    async def test_episodic_skipped_when_no_cue(self) -> None:
        store = _FakeStore(semantic_hits=[_FactHit("f1")])
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="what is my favourite color?",
            query_embedding=[0.1], config=_cfg(episodic=True),
        )
        assert store.semantic_calls == 1
        assert store.episodic_calls == 0


class TestTemporalGating:
    @pytest.mark.asyncio
    async def test_temporal_runs_when_range_provided(self) -> None:
        store = _FakeStore(
            semantic_hits=[_FactHit("f1")],
            temporal_hits=[_FactHit("t1"), _FactHit("t2")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="what happened on May 5th?", query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(datetime(2024, 5, 5), datetime(2024, 5, 5, 23, 59)),
        )
        ids = [h.fact_id for h in out]
        assert "f1" in ids
        assert "t1" in ids and "t2" in ids
        assert store.temporal_calls == 1

    @pytest.mark.asyncio
    async def test_temporal_skipped_when_range_none(self) -> None:
        store = _FakeStore(semantic_hits=[_FactHit("f1")])
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=None,
        )
        assert store.temporal_calls == 0


# ────────────────────────────────────────────────────────────────────────
# RRF fusion behavior
# ────────────────────────────────────────────────────────────────────────


class TestRRFFusion:
    @pytest.mark.asyncio
    async def test_dedupe_by_fact_id(self) -> None:
        """A fact returned by multiple branches appears once in the output."""
        shared = _FactHit("shared-id")
        store = _FakeStore(
            semantic_hits=[shared, _FactHit("f1")],
            episodic_hits=[shared, _FactHit("e1")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="where did I meet Bob?", query_embedding=[0.1],
            config=_cfg(episodic=True),
        )
        ids = [h.fact_id for h in out]
        assert ids.count("shared-id") == 1
        assert set(ids) == {"shared-id", "f1", "e1"}

    @pytest.mark.asyncio
    async def test_cross_strategy_agreement_ranks_above_single_strategy(self) -> None:
        """A fact that appears in BOTH semantic and temporal must outrank
        a fact that only appears in semantic at the same rank. This is
        the core RRF property that makes the fusion robust to false-
        positive temporal hits."""
        # 'shared' is rank-0 in both semantic and temporal
        # 'only_semantic' is rank-0 in semantic only
        shared = _FactHit("shared")
        only_semantic = _FactHit("only_semantic")
        only_temporal = _FactHit("only_temporal")
        store = _FakeStore(
            semantic_hits=[shared, only_semantic],
            temporal_hits=[shared, only_temporal],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(datetime(2024, 5, 5), datetime(2024, 5, 6)),
        )
        ids = [h.fact_id for h in out]
        # 'shared' must be first (it scores 1/61 + 1/61 = ~0.0328)
        # 'only_semantic' and 'only_temporal' each score 1/61 = ~0.0164
        assert ids[0] == "shared"
        assert set(ids[1:]) == {"only_semantic", "only_temporal"}

    @pytest.mark.asyncio
    async def test_higher_rank_in_one_strategy_beats_lower_rank_in_two(self) -> None:
        """RRF k=60 means very close differences in ranks matter less
        than appearing in multiple strategies, but a top-1 in semantic
        still beats a rank-5 + rank-5 pairing.

        rank-0 in semantic alone: 1/61 ≈ 0.01639
        rank-4 + rank-4 in semantic + temporal: 1/65 + 1/65 ≈ 0.03077
        → multi-strategy wins here
        """
        # Test the boundary: a fact at very low rank in two strategies vs
        # rank-0 in one
        top_in_sem = _FactHit("top_in_sem")
        mid_in_both = _FactHit("mid_in_both")
        store = _FakeStore(
            semantic_hits=[top_in_sem] + [_FactHit(f"pad_s{i}") for i in range(4)] + [mid_in_both],
            temporal_hits=[_FactHit(f"pad_t{i}") for i in range(4)] + [mid_in_both],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(datetime(2024, 5, 5), datetime(2024, 5, 6)),
        )
        ids = [h.fact_id for h in out]
        # mid_in_both: 1/65 + 1/65 ≈ 0.03077
        # top_in_sem: 1/61 ≈ 0.01639
        # → mid_in_both should outrank top_in_sem
        assert ids.index("mid_in_both") < ids.index("top_in_sem")

    @pytest.mark.asyncio
    async def test_hits_without_fact_id_are_dropped(self) -> None:
        """A hit object missing ``fact_id`` can't be deduped safely;
        the fuser drops it rather than risk inflating the candidate pool."""
        good = _FactHit("good")
        bad = _FactHit("bad")
        bad.fact_id = None  # type: ignore[assignment]
        store = _FakeStore(semantic_hits=[good, bad])
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
        )
        assert [h.fact_id for h in out] == ["good"]


# ────────────────────────────────────────────────────────────────────────
# Failure isolation
# ────────────────────────────────────────────────────────────────────────


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_semantic_failure_returns_other_branches(self) -> None:
        store = _FakeStore(
            episodic_hits=[_FactHit("e1")],
            temporal_hits=[_FactHit("t1")],
            fail_semantic=True,
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="where did I meet?", query_embedding=[0.1],
            config=_cfg(episodic=True),
            temporal_range=(datetime(2024, 5, 5), datetime(2024, 5, 6)),
        )
        ids = [h.fact_id for h in out]
        assert set(ids) == {"e1", "t1"}

    @pytest.mark.asyncio
    async def test_episodic_failure_returns_semantic(self) -> None:
        store = _FakeStore(
            semantic_hits=[_FactHit("f1")], fail_episodic=True,
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="where did I meet?", query_embedding=[0.1],
            config=_cfg(episodic=True),
        )
        assert [h.fact_id for h in out] == ["f1"]

    @pytest.mark.asyncio
    async def test_temporal_failure_returns_semantic(self) -> None:
        store = _FakeStore(
            semantic_hits=[_FactHit("f1")], fail_temporal=True,
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(datetime(2024, 5, 5), datetime(2024, 5, 6)),
        )
        assert [h.fact_id for h in out] == ["f1"]


# ────────────────────────────────────────────────────────────────────────
# M27 — link-expansion (cross-session entity graph traversal)
# ────────────────────────────────────────────────────────────────────────


class TestLinkExpansionGating:
    @pytest.mark.asyncio
    async def test_link_expansion_skipped_when_no_query_entities(self) -> None:
        """No entities → no link-expansion branch fires. Lazy gate."""
        store = _FakeStore(semantic_hits=[_FactHit("f1")])
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
            query_entities=None,
        )
        assert store.link_expansion_calls == 0

    @pytest.mark.asyncio
    async def test_link_expansion_skipped_when_empty_entity_list(self) -> None:
        store = _FakeStore(semantic_hits=[_FactHit("f1")])
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
            query_entities=[],
        )
        assert store.link_expansion_calls == 0

    @pytest.mark.asyncio
    async def test_link_expansion_fires_per_query_entity(self) -> None:
        """Each query entity triggers a separate search_facts_by_entity call."""
        store = _FakeStore(
            semantic_hits=[_FactHit("f1")],
            link_hits_by_entity={
                "Alice": [_FactHit("a1"), _FactHit("a2")],
                "Bob": [_FactHit("b1")],
            },
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="who met Alice and Bob?", query_embedding=[0.1],
            config=_cfg(episodic=False),
            query_entities=["Alice", "Bob"],
        )
        assert store.link_expansion_calls == 2
        assert set(store.link_expansion_entities) == {"Alice", "Bob"}
        ids = [h.fact_id for h in out]
        # All link-expansion hits surface in the fused output
        for expected in ("a1", "a2", "b1", "f1"):
            assert expected in ids, f"{expected} missing from {ids}"

    @pytest.mark.asyncio
    async def test_link_expansion_dedups_by_fact_id(self) -> None:
        """If two entities both return the same fact, it appears once in output."""
        shared = _FactHit("shared-fact")
        store = _FakeStore(
            semantic_hits=[],
            link_hits_by_entity={
                "Alice": [shared, _FactHit("a1")],
                "Bob": [shared, _FactHit("b1")],
            },
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="anything", query_embedding=[0.1],
            config=_cfg(episodic=False),
            query_entities=["Alice", "Bob"],
        )
        ids = [h.fact_id for h in out]
        # `shared-fact` should appear exactly once despite being in both
        # per-entity result lists.
        assert ids.count("shared-fact") == 1
        assert "a1" in ids and "b1" in ids

    @pytest.mark.asyncio
    async def test_link_expansion_passes_document_id_none(self) -> None:
        """The link-expansion branch MUST query without a document_id
        filter — that's the cross-session-traversal whole point."""
        captured_doc_ids: list = []

        class _CaptureStore(_FakeStore):
            async def search_facts_by_entity(
                self, bank_id, entity, *, top_k, document_id, fact_type=None, session_filter=None,
            ):
                captured_doc_ids.append((entity, document_id))
                return await super().search_facts_by_entity(
                    bank_id, entity, top_k=top_k, document_id=document_id,
                    fact_type=fact_type, session_filter=session_filter,
                )

        store = _CaptureStore(
            semantic_hits=[_FactHit("f1")],
            link_hits_by_entity={"Alice": [_FactHit("a1")]},
        )
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",  # outer doc filter
            query="x", query_embedding=[0.1],
            config=_cfg(episodic=False),
            query_entities=["Alice"],
        )
        # Verify the link-expansion call (for "Alice") used document_id=None
        # even though fact_recall was called with document_id="d1".
        alice_calls = [d for e, d in captured_doc_ids if e == "Alice"]
        assert alice_calls == [None]


class TestLinkExpansionFusion:
    @pytest.mark.asyncio
    async def test_link_expansion_composes_with_semantic(self) -> None:
        """Semantic + link-expansion both contribute facts to fused result."""
        store = _FakeStore(
            semantic_hits=[_FactHit("sem1"), _FactHit("sem2")],
            link_hits_by_entity={"Alice": [_FactHit("link1")]},
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="alice", query_embedding=[0.1],
            config=_cfg(episodic=False),
            query_entities=["Alice"],
        )
        ids = {h.fact_id for h in out}
        assert {"sem1", "sem2", "link1"} <= ids

    @pytest.mark.asyncio
    async def test_link_expansion_failure_isolated(self) -> None:
        """Per-branch failure isolation — link-expansion errors don't
        knock out semantic. Internal _safe_call wraps each entity's
        search; one entity failing leaves the others' results intact."""

        call_count = [0]

        class _FlakyStore(_FakeStore):
            async def search_facts_by_entity(
                self, bank_id, entity, *, top_k, document_id, fact_type=None, session_filter=None,
            ):
                # Episodic still uses this method too; only fail when
                # called for the specific "BadEntity" link-expansion path.
                if entity == "BadEntity":
                    call_count[0] += 1
                    raise RuntimeError("link search blew up")
                return await super().search_facts_by_entity(
                    bank_id, entity, top_k=top_k, document_id=document_id,
                    fact_type=fact_type, session_filter=session_filter,
                )

        store = _FlakyStore(
            semantic_hits=[_FactHit("sem1")],
            link_hits_by_entity={"Alice": [_FactHit("a1")]},
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="x", query_embedding=[0.1],
            config=_cfg(episodic=False),
            query_entities=["Alice", "BadEntity"],
        )
        ids = {h.fact_id for h in out}
        # Semantic + Alice survive even though BadEntity failed.
        assert "sem1" in ids
        assert "a1" in ids
        assert call_count[0] == 1  # BadEntity tried exactly once


# ────────────────────────────────────────────────────────────────────────
# M31 Fix 2 — session_filter threading
# ────────────────────────────────────────────────────────────────────────


class TestSessionFilterThreading:
    """The ``session_filter`` parameter on ``fact_recall`` must propagate
    to semantic / episodic / temporal branches, but DELIBERATELY NOT to
    link-expansion (whose purpose is cross-session entity traversal).
    """

    @pytest.mark.asyncio
    async def test_session_filter_threads_to_semantic(self) -> None:
        store = _FakeStore(semantic_hits=[_FactHit("s1")])
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="x", query_embedding=[0.1],
            config=_cfg(episodic=False),
            session_filter="session-abc",
        )
        assert store.last_session_filter_semantic == "session-abc"

    @pytest.mark.asyncio
    async def test_session_filter_threads_to_episodic(self) -> None:
        store = _FakeStore(
            semantic_hits=[_FactHit("s1")],
            episodic_hits=[_FactHit("e1")],
        )
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="what did I do at the meeting",  # matches _EVENT_CUE_RE
            query_embedding=[0.1],
            config=_cfg(episodic=True),
            session_filter="session-abc",
        )
        assert store.last_session_filter_episodic == "session-abc"

    @pytest.mark.asyncio
    async def test_session_filter_threads_to_temporal(self) -> None:
        from datetime import datetime, timezone

        store = _FakeStore(
            semantic_hits=[_FactHit("s1")],
            temporal_hits=[_FactHit("t1")],
        )
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="x", query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                datetime(2024, 12, 31, tzinfo=timezone.utc),
            ),
            session_filter="session-abc",
        )
        assert store.last_session_filter_temporal == "session-abc"

    @pytest.mark.asyncio
    async def test_session_filter_excluded_from_link_expansion(self) -> None:
        """link-expansion's whole point is cross-session entity traversal —
        constraining it to one session defeats the strategy."""
        store = _FakeStore(
            semantic_hits=[_FactHit("s1")],
            link_hits_by_entity={"Alice": [_FactHit("link1")]},
        )
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="who did I discuss alice with",
            query_embedding=[0.1],
            config=_cfg(episodic=False),
            query_entities=["Alice"],
            session_filter="session-abc",
        )
        # Semantic gets the filter; link-expansion does NOT.
        assert store.last_session_filter_semantic == "session-abc"
        assert store.last_session_filter_link is None

    @pytest.mark.asyncio
    async def test_session_filter_default_none_preserves_v014_behaviour(self) -> None:
        """Backward-compat: callers that don't pass session_filter must
        see store invoked with session_filter=None (no behaviour change
        from v0.14.0)."""
        store = _FakeStore(semantic_hits=[_FactHit("s1")])
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="x", query_embedding=[0.1],
            config=_cfg(episodic=False),
        )
        assert store.last_session_filter_semantic is None


# ────────────────────────────────────────────────────────────────────────
# M31c — BM25 fact-keyword sibling
#
# IMPLEMENTED + BENCHED + REVERTED FROM DEFAULT PATH (v015e: net −6.7pp
# LME top_50 due to anti-composition with synthesis-heavy categories).
# The ``store.search_facts_keyword`` SPI method ships as opt-in primitive
# for custom retrieval pipelines but is NOT wired into the default
# ``fact_recall`` RRF fan-out. See m31-lme-quality.md §8 for the post-
# mortem. The single test below confirms ``fact_recall`` does NOT
# invoke the keyword branch by default (back-compat with the v015d
# baseline that v0.15.0 ships against).
# ────────────────────────────────────────────────────────────────────────


class TestKeywordBranchOptInOnly:
    @pytest.mark.asyncio
    async def test_keyword_branch_not_invoked_by_default(self) -> None:
        """v015e bench showed BM25-as-5th-RRF-sibling anti-composed
        with synthesis-heavy categories (−20pp multi-session, −13pp
        temporal). Default ``fact_recall`` must not call the keyword
        branch even when the store supports it."""
        store = _FakeStore(
            semantic_hits=[_FactHit("sem1")],
            keyword_hits=[_FactHit("kw1")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="philips led bulb",
            query_embedding=[0.1],
            config=_cfg(episodic=False),
        )
        # search_facts_keyword MUST NOT be invoked by default.
        assert store.keyword_calls == 0
        # Semantic still fires; keyword hits absent from fused output.
        ids = {h.fact_id for h in out}
        assert "sem1" in ids
        assert "kw1" not in ids


class TestM34PerFactTypeSegmentation:
    """M34-4 — when ``fact_types`` is provided, each type gets its own
    fused pool. Per-type results are merged so a flood in one channel's
    output (e.g. temporal returning experience facts) can't displace
    relevant hits of other types (e.g. preference)."""

    @pytest.mark.asyncio
    async def test_no_fact_types_preserves_single_pool_behaviour(self) -> None:
        # BC: caller that doesn't pass fact_types gets the pre-M34 path.
        store = _FakeStore(
            semantic_hits=[
                _FactHit("a", fact_type="experience"),
                _FactHit("b", fact_type="preference"),
            ],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="x", query_embedding=[0.1],
            config=_cfg(episodic=False),
            # no fact_types → single pool
        )
        assert {h.fact_id for h in out} == {"a", "b"}

    @pytest.mark.asyncio
    async def test_per_type_segmentation_runs_channels_per_type(self) -> None:
        # When fact_types=['experience','preference'], the store's
        # search_facts_semantic is called twice (once per type) and the
        # type filter is propagated each call.
        store = _FakeStore(
            semantic_hits=[
                _FactHit("a", fact_type="experience"),
                _FactHit("b", fact_type="preference"),
            ],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="x", query_embedding=[0.1],
            config=_cfg(episodic=False),
            fact_types=["experience", "preference"],
        )
        # Two channel invocations (one per type)
        assert store.semantic_calls == 2
        # Output contains both — each type contributed its own pool
        assert {h.fact_id for h in out} == {"a", "b"}

    @pytest.mark.asyncio
    async def test_per_type_segmentation_isolates_flood(self) -> None:
        # The core M34 win: when one channel floods with one fact_type,
        # the other type's pool is NOT crowded out. Semantic returns 5
        # experience facts and 1 preference fact; without segmentation
        # the preference fact would rank deeply against the experience
        # flood. With segmentation, the preference fact gets its own
        # pool and surfaces alongside the experience hits.
        store = _FakeStore(
            semantic_hits=[
                _FactHit(f"exp{i}", fact_type="experience") for i in range(5)
            ] + [_FactHit("pref1", fact_type="preference")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="x", query_embedding=[0.1],
            config=_cfg(episodic=False),
            fact_types=["experience", "preference"],
            max_tokens=20,  # tiny budget — forces interleave + early truncation
        )
        # max_tokens=20 with 2 types and round-robin interleave:
        # order is [exp0, pref1, exp1, exp2, ...]. The preference fact
        # MUST appear in the output even though experience has 5x the
        # candidates — that's the whole point of segmentation: a
        # flood in one fact_type can't displace the other type's
        # top hit.
        ids = [h.fact_id for h in out]
        assert "pref1" in ids, f"preference fact crowded out by flood: {ids}"


class TestM34BM25IntentGating:
    """M34-5 — BM25 keyword channel fires only when:
      1. caller passes an intent (i.e. opted in to intent-aware routing), AND
      2. that intent's bm25 weight > 0.

    Pre-M34 BC: when intent is None, BM25 stays off entirely (the
    M31c regression analysis stands until intent gating is in play)."""

    @pytest.mark.asyncio
    async def test_bm25_off_when_intent_is_none(self) -> None:
        from astrocyte.pipeline.query_intent import QueryIntent  # noqa

        store = _FakeStore(
            semantic_hits=[_FactHit("sem")],
            keyword_hits=[_FactHit("kw")],
        )
        await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="x", query_embedding=[0.1],
            config=_cfg(episodic=False),
            # no intent → BM25 stays off (BC)
        )
        assert store.keyword_calls == 0

    @pytest.mark.asyncio
    async def test_bm25_fires_when_intent_factual(self) -> None:
        # FACTUAL intent → bm25 weight 1.5 → channel fires.
        from astrocyte.pipeline.query_intent import QueryIntent

        store = _FakeStore(
            semantic_hits=[_FactHit("sem")],
            keyword_hits=[_FactHit("kw")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="what is my X", query_embedding=[0.1],
            config=_cfg(episodic=False),
            intent=QueryIntent.FACTUAL,
        )
        assert store.keyword_calls == 1
        ids = {h.fact_id for h in out}
        assert "kw" in ids  # BM25 hit reached the fused output


class TestM34IntentWeightedFusion:
    """M34-2 — when ``intent`` is passed, fused ranking reflects the
    intent's per-channel weights instead of equal-weight RRF."""

    @pytest.mark.asyncio
    async def test_no_intent_preserves_equal_weight_behaviour(self) -> None:
        # BC: caller that doesn't pass intent gets the pre-M34 ranking.
        # Two channels, each with one unique hit. Equal weights → both
        # land at the same fused score (1/61 ≈ 0.0164). Order is then
        # determined by first-seen, which is semantic.
        store = _FakeStore(
            semantic_hits=[_FactHit("sem")],
            temporal_hits=[_FactHit("temp")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="last week",
            query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(datetime(2023, 1, 1), datetime(2023, 1, 7)),
            # no intent → equal-weight path
        )
        ids = [h.fact_id for h in out]
        assert set(ids) == {"sem", "temp"}

    @pytest.mark.asyncio
    async def test_temporal_intent_boosts_temporal_hit_above_semantic(self) -> None:
        # TEMPORAL intent: temporal weight=1.5, semantic weight=1.0.
        # Both channels return one hit each at rank 0. Fused scores:
        #   sem  = 1.0 / 61 ≈ 0.0164
        #   temp = 1.5 / 61 ≈ 0.0246
        # Temporal hit must rank first.
        from astrocyte.pipeline.query_intent import QueryIntent

        store = _FakeStore(
            semantic_hits=[_FactHit("sem")],
            temporal_hits=[_FactHit("temp")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="how many weeks ago did I do X",
            query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(datetime(2023, 1, 1), datetime(2023, 1, 7)),
            intent=QueryIntent.TEMPORAL,
        )
        ids = [h.fact_id for h in out]
        assert ids[0] == "temp", f"expected temp first, got {ids}"

    @pytest.mark.asyncio
    async def test_factual_intent_damps_temporal_below_semantic(self) -> None:
        # FACTUAL intent: semantic weight=1.5, temporal weight=0.3.
        # Same input as above; now semantic must rank first because
        # the temporal channel's contribution is heavily damped.
        from astrocyte.pipeline.query_intent import QueryIntent

        store = _FakeStore(
            semantic_hits=[_FactHit("sem")],
            temporal_hits=[_FactHit("temp")],
        )
        out = await fact_recall(
            store=store, bank_id="b1", document_id="d1",
            query="what is my favourite coffee",
            query_embedding=[0.1],
            config=_cfg(episodic=False),
            temporal_range=(datetime(2023, 1, 1), datetime(2023, 1, 7)),
            intent=QueryIntent.FACTUAL,
        )
        ids = [h.fact_id for h in out]
        assert ids[0] == "sem", f"expected sem first, got {ids}"
