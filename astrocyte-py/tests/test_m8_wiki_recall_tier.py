"""M8 W5: Wiki-tier recall precedence — unit tests.

Tests cover:
- Wiki tier engaged when top wiki hit >= wiki_confidence_threshold
- Wiki tier skipped when top wiki hit < threshold (falls through to standard recall)
- Wiki tier skipped when no wiki pages exist in the bank
- Wiki tier skipped when caller sets fact_types (explicit raw-memory query)
- Citation raw memories appended from _wiki_source_ids metadata
- RecallTrace.wiki_tier_used flag set correctly
- wiki_store=None → wiki tier never runs (standard recall only)
- wiki_confidence_threshold configurable
- Token budget applied to wiki-tier result
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, InMemoryWikiStore, MockLLMProvider
from astrocyte.types import RecallRequest, VectorItem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIM = 16  # small embeddings for tests


def _unit_vec(pos: int, dim: int = _DIM) -> list[float]:
    """Return a unit vector with 1.0 at position pos%dim, 0 elsewhere."""
    v = [0.0] * dim
    v[pos % dim] = 1.0
    return v


def _wiki_item(
    page_id: str,
    bank_id: str,
    title: str,
    content: str,
    vector_pos: int,
    source_ids: list[str] | None = None,
) -> VectorItem:
    """Create a VectorItem representing a compiled wiki page."""
    meta = {}
    if source_ids:
        meta["_wiki_source_ids"] = ",".join(source_ids)
    return VectorItem(
        id=page_id,
        bank_id=bank_id,
        vector=_unit_vec(vector_pos),
        text=f"[WIKI:topic] {title}\n\n{content}",
        memory_layer="compiled",
        fact_type="wiki",
        metadata=meta or None,
    )


def _raw_item(item_id: str, bank_id: str, text: str, vector_pos: int) -> VectorItem:
    """Create a VectorItem representing a raw memory."""
    return VectorItem(
        id=item_id,
        bank_id=bank_id,
        vector=_unit_vec(vector_pos),
        text=text,
        memory_layer="fact",
        fact_type="world",
    )


def _orchestrator(
    vs: InMemoryVectorStore,
    llm: MockLLMProvider,
    wiki_store: InMemoryWikiStore | None,
    threshold: float = 0.7,
) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        vs,
        llm,
        wiki_store=wiki_store,
        wiki_confidence_threshold=threshold,
    )


# MockLLMProvider.embed returns a fixed vector regardless of input — we need
# to control what vector the query embeds to so we can make wiki hits score
# highly.  We subclass to return a controllable vector.

class _VecLLMProvider(MockLLMProvider):
    """LLM provider whose embed() returns a preset vector for any input."""

    def __init__(self, query_vec: list[float]) -> None:
        super().__init__()
        self._query_vec = query_vec

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:  # type: ignore[override]
        return [self._query_vec] * len(texts)


# ---------------------------------------------------------------------------
# Wiki tier engaged
# ---------------------------------------------------------------------------


class TestWikiTierEngaged:
    """Wiki hits score >= threshold → return wiki tier result without full recall."""

    @pytest.mark.asyncio
    async def test_wiki_tier_returns_hits(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()
        # Store a wiki page whose vector is identical to the query vector (score=1.0)
        query_vec = _unit_vec(0)
        wiki = _wiki_item("topic:incident-response", "bank1", "Incident Response", "content", 0)
        await vs.store_vectors([wiki])

        llm = _VecLLMProvider(query_vec)
        orch = _orchestrator(vs, llm, ws, threshold=0.7)

        result = await orch.recall(RecallRequest(query="incident response", bank_id="bank1", max_results=5))

        assert len(result.hits) >= 1
        assert result.hits[0].fact_type == "wiki"
        assert result.hits[0].memory_layer == "compiled"

    @pytest.mark.asyncio
    async def test_wiki_tier_used_flag_set(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()
        query_vec = _unit_vec(0)
        wiki = _wiki_item("topic:incident-response", "bank1", "Incident Response", "content", 0)
        await vs.store_vectors([wiki])

        llm = _VecLLMProvider(query_vec)
        orch = _orchestrator(vs, llm, ws, threshold=0.7)

        result = await orch.recall(RecallRequest(query="incident response", bank_id="bank1", max_results=5))

        assert result.trace is not None
        assert result.trace.wiki_tier_used is True
        assert result.trace.strategies_used == ["wiki"]
        assert result.trace.fusion_method == "wiki_tier"

    @pytest.mark.asyncio
    async def test_citations_appended_from_source_ids(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        # Store two raw memories that are cited by the wiki page
        raw1 = _raw_item("raw-001", "bank1", "Alert fired at 03:00", 5)
        raw2 = _raw_item("raw-002", "bank1", "Resolved by oncall in 20 min", 6)
        await vs.store_vectors([raw1, raw2])

        # Wiki page with source_ids pointing at those raw memories
        query_vec = _unit_vec(0)
        wiki = _wiki_item(
            "topic:incident-response",
            "bank1",
            "Incident Response",
            "summary of incident",
            0,
            source_ids=["raw-001", "raw-002"],
        )
        await vs.store_vectors([wiki])

        llm = _VecLLMProvider(query_vec)
        orch = _orchestrator(vs, llm, ws, threshold=0.7)

        result = await orch.recall(RecallRequest(query="incident response", bank_id="bank1", max_results=20))

        # Should have wiki hit(s) + raw citation hits
        memory_ids = {h.memory_id for h in result.hits}
        assert "topic:incident-response" in memory_ids
        assert "raw-001" in memory_ids
        assert "raw-002" in memory_ids

        # Citation hits carry memory_layer="raw"
        citations = [h for h in result.hits if h.memory_layer == "raw"]
        assert len(citations) == 2
        # Citations have score=0.0 (provenance, not ranked)
        assert all(c.score == 0.0 for c in citations)

    @pytest.mark.asyncio
    async def test_citations_missing_source_ids_silently_skipped(self):
        """If _wiki_source_ids metadata is absent, no citations are returned."""
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        query_vec = _unit_vec(0)
        # Wiki page with no source_ids in metadata
        wiki = _wiki_item("topic:faq", "bank1", "FAQ", "answers", 0, source_ids=None)
        await vs.store_vectors([wiki])

        llm = _VecLLMProvider(query_vec)
        orch = _orchestrator(vs, llm, ws, threshold=0.7)

        result = await orch.recall(RecallRequest(query="faq", bank_id="bank1", max_results=5))

        # Still returns wiki hit; just no citation hits
        assert any(h.fact_type == "wiki" for h in result.hits)
        assert not any(h.memory_layer == "raw" for h in result.hits)


# ---------------------------------------------------------------------------
# Wiki tier skipped
# ---------------------------------------------------------------------------


class TestWikiTierSkipped:
    """Wiki score below threshold or no wiki pages → fall through to full recall."""

    @pytest.mark.asyncio
    async def test_below_threshold_falls_through(self):
        """Query vector orthogonal to wiki vector → score near 0, falls through."""
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        # Wiki page at position 0; query at position 1 → cosine similarity = 0
        wiki = _wiki_item("topic:incident-response", "bank1", "Incident Response", "content", 0)
        await vs.store_vectors([wiki])

        # Store a raw memory at position 1 that matches the query
        raw = _raw_item("raw-001", "bank1", "Something relevant", 1)
        await vs.store_vectors([raw])

        query_vec = _unit_vec(1)  # orthogonal to wiki
        llm = _VecLLMProvider(query_vec)
        orch = _orchestrator(vs, llm, ws, threshold=0.7)

        result = await orch.recall(RecallRequest(query="something relevant", bank_id="bank1", max_results=5))

        assert result.trace is not None
        assert result.trace.wiki_tier_used is not True  # None or False — not True

    @pytest.mark.asyncio
    async def test_no_wiki_pages_falls_through(self):
        """Empty wiki layer → wiki tier returns None, standard recall runs."""
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        raw = _raw_item("raw-001", "bank1", "Important memory", 0)
        await vs.store_vectors([raw])

        query_vec = _unit_vec(0)
        llm = _VecLLMProvider(query_vec)
        orch = _orchestrator(vs, llm, ws, threshold=0.7)

        result = await orch.recall(RecallRequest(query="important", bank_id="bank1", max_results=5))

        # Standard recall ran — trace shows normal strategies (not wiki)
        assert result.trace is not None
        assert result.trace.wiki_tier_used is not True

    @pytest.mark.asyncio
    async def test_no_wiki_store_skips_tier_entirely(self):
        """wiki_store=None → wiki tier code path never executes."""
        vs = InMemoryVectorStore()

        # Even if a "wiki" fact_type item exists, it should NOT be returned
        # via wiki-tier short-circuit because wiki_store is None.
        wiki = _wiki_item("topic:test", "bank1", "Test", "content", 0)
        raw = _raw_item("raw-001", "bank1", "A raw memory", 0)
        await vs.store_vectors([wiki, raw])

        query_vec = _unit_vec(0)
        llm = _VecLLMProvider(query_vec)
        # No wiki_store passed
        orch = PipelineOrchestrator(vs, llm)

        result = await orch.recall(RecallRequest(query="test", bank_id="bank1", max_results=5))

        # Standard recall ran — wiki tier was skipped
        assert result.trace is not None
        assert result.trace.wiki_tier_used is not True

    @pytest.mark.asyncio
    async def test_explicit_fact_types_bypasses_wiki_tier(self):
        """Caller setting fact_types signals they want raw memories; wiki tier skipped."""
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        query_vec = _unit_vec(0)
        wiki = _wiki_item("topic:incident-response", "bank1", "Incident Response", "content", 0)
        raw = _raw_item("raw-001", "bank1", "raw memory text", 0)
        await vs.store_vectors([wiki, raw])

        llm = _VecLLMProvider(query_vec)
        orch = _orchestrator(vs, llm, ws, threshold=0.7)

        # Caller explicitly requests raw world facts only
        result = await orch.recall(
            RecallRequest(query="incident response", bank_id="bank1", max_results=5, fact_types=["world"])
        )

        assert result.trace is not None
        assert result.trace.wiki_tier_used is not True
        # No wiki hits in result
        assert not any(h.fact_type == "wiki" for h in result.hits)


# ---------------------------------------------------------------------------
# Configurable threshold
# ---------------------------------------------------------------------------


class TestWikiConfidenceThreshold:
    @pytest.mark.asyncio
    async def test_high_threshold_not_met(self):
        """Score of 1.0 still passes threshold=1.0 exactly."""
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()
        query_vec = _unit_vec(0)
        wiki = _wiki_item("topic:foo", "bank1", "Foo", "bar", 0)
        await vs.store_vectors([wiki])

        llm = _VecLLMProvider(query_vec)
        orch = _orchestrator(vs, llm, ws, threshold=1.0)

        result = await orch.recall(RecallRequest(query="foo", bank_id="bank1", max_results=5))
        # score == 1.0 >= threshold == 1.0 → wiki tier engaged
        assert result.trace is not None
        assert result.trace.wiki_tier_used is True

    @pytest.mark.asyncio
    async def test_low_threshold_always_engages(self):
        """Threshold=0.0 means any wiki hit engages the tier."""
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        # Orthogonal vectors → score=0.0; but threshold=0.0 so still engaged
        wiki = _wiki_item("topic:foo", "bank1", "Foo", "bar", 0)
        await vs.store_vectors([wiki])

        query_vec = _unit_vec(1)
        llm = _VecLLMProvider(query_vec)
        orch = _orchestrator(vs, llm, ws, threshold=0.0)

        result = await orch.recall(RecallRequest(query="foo", bank_id="bank1", max_results=5))
        # Score 0.0 >= threshold 0.0 → wiki tier engaged
        assert result.trace is not None
        assert result.trace.wiki_tier_used is True


# ---------------------------------------------------------------------------
# Token budget applied in wiki tier
# ---------------------------------------------------------------------------


class TestWikiTierTokenBudget:
    @pytest.mark.asyncio
    async def test_token_budget_truncates_wiki_result(self):
        """max_tokens applies to wiki-tier results the same as standard recall."""
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        query_vec = _unit_vec(0)
        # Create a wiki page with very long text to trigger token budget
        long_content = "word " * 3000  # ~3000 tokens
        wiki = _wiki_item("topic:long", "bank1", "Long Article", long_content, 0)
        await vs.store_vectors([wiki])

        llm = _VecLLMProvider(query_vec)
        orch = _orchestrator(vs, llm, ws, threshold=0.7)

        result = await orch.recall(
            RecallRequest(query="long", bank_id="bank1", max_results=5, max_tokens=50)
        )

        assert result.trace is not None
        assert result.trace.wiki_tier_used is True
        # Result should be truncated (truncated flag set or no hits)
        assert result.truncated is True or len(result.hits) == 0


# ---------------------------------------------------------------------------
# Bank isolation
# ---------------------------------------------------------------------------


class TestWikiTierBankIsolation:
    @pytest.mark.asyncio
    async def test_wiki_pages_in_different_bank_not_returned(self):
        """Wiki page in bank2 must not surface during bank1 recall."""
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        query_vec = _unit_vec(0)
        # Wiki page stored in bank2
        wiki_b2 = _wiki_item("topic:foo", "bank2", "Foo", "content", 0)
        await vs.store_vectors([wiki_b2])

        # Raw memory in bank1 at same vector position
        raw_b1 = _raw_item("raw-001", "bank1", "bank1 memory", 0)
        await vs.store_vectors([raw_b1])

        llm = _VecLLMProvider(query_vec)
        orch = _orchestrator(vs, llm, ws, threshold=0.7)

        result = await orch.recall(RecallRequest(query="foo", bank_id="bank1", max_results=5))

        # Wiki tier should not have engaged (wrong bank)
        wiki_hits = [h for h in result.hits if h.fact_type == "wiki"]
        assert len(wiki_hits) == 0
