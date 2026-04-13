"""Unit tests for pipeline/tiered_retrieval.py — progressive escalation paths.

Covers all 5 tiers (cache, fuzzy recent, BM25, full multi-strategy, agentic),
escalation logic, cache interactions, external context merge, reformulation
fallback, and edge cases.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.pipeline.recall_cache import RecallCache
from astrocyte.pipeline.recent_buffer import RecentMemoryBuffer
from astrocyte.pipeline.tiered_retrieval import TieredRetriever
from astrocyte.testing.in_memory import (
    InMemoryDocumentStore,
    InMemoryVectorStore,
    MockLLMProvider,
)
from astrocyte.types import (
    Document,
    MemoryHit,
    RecallRequest,
    RecallResult,
    RecallTrace,
    RetainRequest,
)


def _make_pipeline(
    *,
    with_doc_store: bool = False,
) -> tuple[PipelineOrchestrator, InMemoryVectorStore, InMemoryDocumentStore | None]:
    vs = InMemoryVectorStore()
    llm = MockLLMProvider()
    ds = InMemoryDocumentStore() if with_doc_store else None
    orch = PipelineOrchestrator(vs, llm, document_store=ds)
    return orch, vs, ds


async def _seed_vectors(pipeline: PipelineOrchestrator, texts: list[str], bank: str = "b1") -> None:
    for text in texts:
        await pipeline.retain(RetainRequest(content=text, bank_id=bank))


async def _seed_documents(ds: InMemoryDocumentStore, texts: list[str], bank: str = "b1") -> None:
    for i, text in enumerate(texts):
        await ds.store_document(Document(id=f"doc-{i}", text=text), bank)


# ---------------------------------------------------------------------------
# Tier 0: Cache hits
# ---------------------------------------------------------------------------


class TestTier0Cache:
    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_result(self):
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA"])
        cache = RecallCache(similarity_threshold=0.9)

        retriever = TieredRetriever(pipeline, recall_cache=cache, max_tier=3)

        # First call — cache miss, populates cache via tier 3
        r1 = await retriever.retrieve(RecallRequest(query="NASA", bank_id="b1", max_results=5))
        assert r1.trace.tier_used == 3
        assert cache.size("b1") == 1

        # Second call — same query, should hit cache
        r2 = await retriever.retrieve(RecallRequest(query="NASA", bank_id="b1", max_results=5))
        assert r2.trace.cache_hit is True
        assert r2.trace.tier_used == 0
        assert r2.trace.strategies_used == ["cache"]

    @pytest.mark.asyncio
    async def test_cache_disabled_skips_tier_0(self):
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA"])

        retriever = TieredRetriever(pipeline, recall_cache=None, max_tier=3)
        result = await retriever.retrieve(RecallRequest(query="NASA", bank_id="b1", max_results=5))
        assert result.trace.tier_used == 3
        assert result.trace.cache_hit is False

    @pytest.mark.asyncio
    async def test_cache_miss_escalates(self):
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA"])
        cache = RecallCache(similarity_threshold=0.99)  # Very strict — unlikely cache hit

        retriever = TieredRetriever(pipeline, recall_cache=cache, max_tier=3)
        result = await retriever.retrieve(RecallRequest(query="NASA", bank_id="b1", max_results=5))
        # First call is always a miss
        assert result.trace.tier_used == 3

    @pytest.mark.asyncio
    async def test_external_context_bypasses_cache(self):
        """Federated/proxy hits must not use stale cache."""
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA"])
        cache = RecallCache()

        retriever = TieredRetriever(pipeline, recall_cache=cache, max_tier=3)

        # First: seed the cache
        await retriever.retrieve(RecallRequest(query="NASA", bank_id="b1", max_results=5))
        assert cache.size("b1") == 1

        # Second: with external_context — must skip cache
        ext = [MemoryHit(text="External NASA data", score=0.8)]
        result = await retriever.retrieve(RecallRequest(
            query="NASA", bank_id="b1", max_results=5, external_context=ext,
        ))
        assert result.trace.tier_used == 3  # Not 0 (cache)
        assert result.trace.cache_hit is False

    @pytest.mark.asyncio
    async def test_cache_respects_max_results(self):
        """Cached results should be trimmed to max_results."""
        pipeline, vs, _ = _make_pipeline()
        for i in range(5):
            await pipeline.retain(RetainRequest(
                content=f"Fact {i} about science topic {i}", bank_id="b1",
            ))
        cache = RecallCache(similarity_threshold=0.8)

        retriever = TieredRetriever(pipeline, recall_cache=cache, max_tier=3)

        # First call with max_results=10 to populate cache
        await retriever.retrieve(RecallRequest(query="science", bank_id="b1", max_results=10))

        # Second call with smaller max_results
        r2 = await retriever.retrieve(RecallRequest(query="science", bank_id="b1", max_results=2))
        assert r2.trace.cache_hit is True
        assert len(r2.hits) <= 2


# ---------------------------------------------------------------------------
# Tier 1: Fuzzy text match on recent memories
# ---------------------------------------------------------------------------


class TestTier1FuzzyRecent:
    @pytest.mark.asyncio
    async def test_fuzzy_resolves_without_escalation(self):
        """When recent buffer has sufficient fuzzy matches, don't escalate."""
        pipeline, vs, _ = _make_pipeline()
        buf = RecentMemoryBuffer()
        buf.add("b1", "m1", "Alice works at NASA on rocket engines")
        buf.add("b1", "m2", "Alice published a paper about propulsion at NASA")
        buf.add("b1", "m3", "Alice mentors junior engineers at NASA headquarters")

        retriever = TieredRetriever(
            pipeline, recent_buffer=buf, min_results=2, min_score=0.2, max_tier=3,
        )
        result = await retriever.retrieve(RecallRequest(
            query="Alice NASA", bank_id="b1", max_results=5,
        ))
        assert result.trace.tier_used == 1
        assert result.trace.fusion_method == "fuzzy_recent"
        assert "fuzzy_recent" in result.trace.strategies_used
        assert len(result.hits) >= 2

    @pytest.mark.asyncio
    async def test_fuzzy_handles_typos(self):
        """Fuzzy matching should catch typos that BM25 would miss."""
        pipeline, vs, _ = _make_pipeline()
        buf = RecentMemoryBuffer()
        buf.add("b1", "m1", "Alice works at NASA on rocket engines")
        buf.add("b1", "m2", "Bob prefers tea over coffee every morning")
        buf.add("b1", "m3", "Charlie studies quantum physics at MIT")

        retriever = TieredRetriever(
            pipeline, recent_buffer=buf, min_results=1, min_score=0.2, max_tier=3,
        )
        # "Alce" is a typo for "Alice", "NASSA" is a typo for "NASA"
        result = await retriever.retrieve(RecallRequest(
            query="Alce NASSA rockets", bank_id="b1", max_results=5,
        ))
        if result.trace.tier_used == 1:
            # Fuzzy match caught the typos
            assert any("NASA" in h.text for h in result.hits)

    @pytest.mark.asyncio
    async def test_fuzzy_insufficient_escalates_to_tier2(self):
        """When fuzzy returns too few hits, escalate to BM25/tier 3."""
        pipeline, vs, _ = _make_pipeline()
        buf = RecentMemoryBuffer()
        buf.add("b1", "m1", "Alice works at NASA")  # Only 1 match

        retriever = TieredRetriever(
            pipeline, recent_buffer=buf, min_results=3, min_score=0.2, max_tier=3,
        )
        result = await retriever.retrieve(RecallRequest(
            query="Alice NASA", bank_id="b1", max_results=5,
        ))
        assert result.trace.tier_used >= 2  # Escalated past fuzzy

    @pytest.mark.asyncio
    async def test_no_recent_buffer_skips_tier1(self):
        """Without a recent buffer, tier 1 is skipped."""
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA"])

        retriever = TieredRetriever(pipeline, recent_buffer=None, max_tier=3)
        result = await retriever.retrieve(RecallRequest(
            query="NASA", bank_id="b1", max_results=5,
        ))
        assert result.trace.tier_used == 3  # Skipped tier 1

    @pytest.mark.asyncio
    async def test_fuzzy_per_bank_isolation(self):
        """Fuzzy buffer for bank A should not match queries for bank B."""
        pipeline, vs, _ = _make_pipeline()
        buf = RecentMemoryBuffer()
        buf.add("bank-a", "m1", "Alice works at NASA on rocket engines")
        buf.add("bank-a", "m2", "Alice published research about propulsion")
        buf.add("bank-a", "m3", "Alice mentors junior engineers")

        retriever = TieredRetriever(
            pipeline, recent_buffer=buf, min_results=2, min_score=0.2, max_tier=3,
        )
        result = await retriever.retrieve(RecallRequest(
            query="Alice NASA", bank_id="bank-b", max_results=5,
        ))
        # bank-b has no recent memories — should not use tier 1
        assert result.trace.tier_used != 1

    @pytest.mark.asyncio
    async def test_notify_retain_populates_buffer(self):
        """notify_retain should add to the recent buffer."""
        pipeline, vs, _ = _make_pipeline()
        buf = RecentMemoryBuffer()
        retriever = TieredRetriever(
            pipeline, recent_buffer=buf, min_results=1, min_score=0.2, max_tier=3,
        )

        retriever.notify_retain("b1", "m1", "Alice works at NASA on rocket engines")
        retriever.notify_retain("b1", "m2", "Alice published research about propulsion")
        assert buf.size("b1") == 2

        result = await retriever.retrieve(RecallRequest(
            query="Alice NASA", bank_id="b1", max_results=5,
        ))
        assert result.trace.tier_used == 1

    @pytest.mark.asyncio
    async def test_notify_retain_invalidates_cache(self):
        """notify_retain should invalidate the recall cache for the bank."""
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA"])
        cache = RecallCache(similarity_threshold=0.8)

        retriever = TieredRetriever(pipeline, recall_cache=cache, max_tier=3)

        # Populate cache
        await retriever.retrieve(RecallRequest(query="NASA", bank_id="b1", max_results=5))
        assert cache.size("b1") == 1

        # Notify retain — should invalidate cache
        retriever.notify_retain("b1", "m99", "New content about NASA")
        assert cache.size("b1") == 0


# ---------------------------------------------------------------------------
# RecentMemoryBuffer unit tests
# ---------------------------------------------------------------------------


class TestRecentMemoryBuffer:
    def test_add_and_search(self):
        buf = RecentMemoryBuffer()
        buf.add("b1", "m1", "Alice works at NASA")
        results = buf.search("Alice NASA", "b1")
        assert len(results) == 1
        assert results[0].memory_id == "m1"

    def test_search_empty_bank(self):
        buf = RecentMemoryBuffer()
        assert buf.search("anything", "b1") == []

    def test_search_min_score_filter(self):
        buf = RecentMemoryBuffer()
        buf.add("b1", "m1", "Alice works at NASA on rocket engines")
        # Query with no overlap — should not match at high min_score
        results = buf.search("quantum physics", "b1", min_score=0.8)
        assert len(results) == 0

    def test_ring_buffer_evicts_oldest(self):
        buf = RecentMemoryBuffer(max_per_bank=3)
        buf.add("b1", "m1", "first memory about cats")
        buf.add("b1", "m2", "second memory about dogs")
        buf.add("b1", "m3", "third memory about birds")
        buf.add("b1", "m4", "fourth memory about fish")
        assert buf.size("b1") == 3
        # First entry should be evicted
        results = buf.search("cats", "b1", min_score=0.1)
        assert not any(r.memory_id == "m1" for r in results)

    def test_fuzzy_match_typo(self):
        buf = RecentMemoryBuffer()
        buf.add("b1", "m1", "Alice works at NASA headquarters")
        # "Alce" is a typo for "Alice"
        results = buf.search("Alce NASA", "b1", min_score=0.3)
        assert len(results) >= 1

    def test_per_bank_isolation(self):
        buf = RecentMemoryBuffer()
        buf.add("bank-a", "m1", "Alice works at NASA")
        buf.add("bank-b", "m2", "Bob prefers coffee")
        assert buf.size("bank-a") == 1
        assert buf.size("bank-b") == 1
        results = buf.search("Alice", "bank-b")
        assert len(results) == 0

    def test_clear_bank(self):
        buf = RecentMemoryBuffer()
        buf.add("b1", "m1", "Alice works at NASA")
        buf.clear_bank("b1")
        assert buf.size("b1") == 0

    def test_size_total(self):
        buf = RecentMemoryBuffer()
        buf.add("b1", "m1", "memory one")
        buf.add("b2", "m2", "memory two")
        assert buf.size() == 2

    def test_stop_words_ignored(self):
        buf = RecentMemoryBuffer()
        buf.add("b1", "m1", "The quick brown fox jumps over the lazy dog")
        # Query with only stop words — no matches
        results = buf.search("the is are", "b1", min_score=0.1)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Tier 2: BM25 keyword search
# ---------------------------------------------------------------------------


class TestTier2BM25:
    @pytest.mark.asyncio
    async def test_bm25_resolves_without_escalation(self):
        """When BM25 returns sufficient high-scoring hits, don't escalate."""
        pipeline, vs, ds = _make_pipeline(with_doc_store=True)
        await _seed_documents(ds, [
            "Alice works at NASA on rocket engines",
            "Alice published a paper about propulsion",
            "Alice presented at the space conference",
            "Alice mentors junior engineers at NASA",
        ])

        retriever = TieredRetriever(
            pipeline, min_results=3, min_score=0.2, max_tier=3,
        )
        result = await retriever.retrieve(RecallRequest(
            query="Alice NASA", bank_id="b1", max_results=5,
        ))
        assert result.trace.tier_used == 2
        assert result.trace.fusion_method == "bm25_only"
        assert "keyword" in result.trace.strategies_used

    @pytest.mark.asyncio
    async def test_bm25_insufficient_results_escalates(self):
        """When BM25 returns too few hits, escalate to tier 3."""
        pipeline, vs, ds = _make_pipeline(with_doc_store=True)
        # Only 1 document — less than min_results=3
        await _seed_documents(ds, ["Alice works at NASA"])
        await _seed_vectors(pipeline, ["Alice works at NASA"])

        retriever = TieredRetriever(
            pipeline, min_results=3, min_score=0.2, max_tier=3,
        )
        result = await retriever.retrieve(RecallRequest(
            query="Alice NASA", bank_id="b1", max_results=5,
        ))
        assert result.trace.tier_used == 3  # Escalated past BM25

    @pytest.mark.asyncio
    async def test_bm25_low_score_escalates(self):
        """When BM25 avg score is below min_score, escalate."""
        pipeline, vs, ds = _make_pipeline(with_doc_store=True)
        # Documents with minimal query term overlap → low scores
        await _seed_documents(ds, [
            "The weather is sunny today and the birds are singing",
            "Tomorrow will be cloudy with a chance of rain showers",
            "Yesterday was particularly warm for this time of year",
        ])
        await _seed_vectors(pipeline, ["Alice works at NASA"])

        retriever = TieredRetriever(
            pipeline, min_results=1, min_score=0.9, max_tier=3,
        )
        result = await retriever.retrieve(RecallRequest(
            query="Alice NASA rockets", bank_id="b1", max_results=5,
        ))
        # BM25 won't find "Alice NASA" in weather docs → escalates
        assert result.trace.tier_used == 3

    @pytest.mark.asyncio
    async def test_bm25_no_doc_store_skips_to_tier3(self):
        """Without a document store, tier 2 is skipped entirely."""
        pipeline, vs, _ = _make_pipeline(with_doc_store=False)
        await _seed_vectors(pipeline, ["Alice works at NASA"])

        retriever = TieredRetriever(pipeline, max_tier=3)
        result = await retriever.retrieve(RecallRequest(
            query="NASA", bank_id="b1", max_results=5,
        ))
        assert result.trace.tier_used == 3

    @pytest.mark.asyncio
    async def test_bm25_merges_external_context(self):
        """Tier 2 results should include external context when present."""
        pipeline, vs, ds = _make_pipeline(with_doc_store=True)
        await _seed_documents(ds, [
            "Alice works at NASA on rocket engines",
            "Alice published a paper about propulsion",
            "Alice presented at the space conference",
        ])

        ext = [MemoryHit(text="External: Alice won an award", score=0.95)]
        retriever = TieredRetriever(
            pipeline, min_results=2, min_score=0.2, max_tier=3,
        )
        result = await retriever.retrieve(RecallRequest(
            query="Alice NASA", bank_id="b1", max_results=10,
            external_context=ext,
        ))
        # External hit should be merged in
        all_texts = [h.text for h in result.hits]
        assert any("award" in t for t in all_texts)


# ---------------------------------------------------------------------------
# Tier 3: Full multi-strategy recall
# ---------------------------------------------------------------------------


class TestTier3FullRecall:
    @pytest.mark.asyncio
    async def test_tier3_uses_pipeline_recall(self):
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA"])

        retriever = TieredRetriever(pipeline, max_tier=3)
        result = await retriever.retrieve(RecallRequest(
            query="NASA", bank_id="b1", max_results=5,
        ))
        assert result.trace.tier_used == 3
        assert result.trace.cache_hit is False
        assert len(result.hits) >= 1

    @pytest.mark.asyncio
    async def test_tier3_with_custom_full_recall(self):
        """Injected full_recall function should be used instead of pipeline.recall."""
        pipeline, vs, _ = _make_pipeline()

        custom_result = RecallResult(
            hits=[MemoryHit(text="custom result", score=1.0)],
            total_available=1,
            truncated=False,
            trace=RecallTrace(strategies_used=["custom"], total_candidates=1, fusion_method="custom"),
        )

        async def mock_full_recall(req: RecallRequest) -> RecallResult:
            return custom_result

        retriever = TieredRetriever(pipeline, max_tier=3, full_recall=mock_full_recall)
        result = await retriever.retrieve(RecallRequest(
            query="anything", bank_id="b1", max_results=5,
        ))
        assert result.hits[0].text == "custom result"
        assert result.trace.tier_used == 3

    @pytest.mark.asyncio
    async def test_tier3_caches_result(self):
        """Tier 3 results should be cached for future queries."""
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA"])
        cache = RecallCache(similarity_threshold=0.8)

        retriever = TieredRetriever(pipeline, recall_cache=cache, max_tier=3)
        await retriever.retrieve(RecallRequest(query="NASA", bank_id="b1", max_results=5))
        assert cache.size("b1") == 1

    @pytest.mark.asyncio
    async def test_tier3_insufficient_escalates_to_tier4(self):
        """When tier 3 returns too few results and max_tier >= 4, escalate."""
        pipeline, vs, _ = _make_pipeline()
        # Empty bank — tier 3 returns nothing

        retriever = TieredRetriever(pipeline, min_results=3, max_tier=4)
        result = await retriever.retrieve(RecallRequest(
            query="nonexistent topic", bank_id="b1", max_results=5,
        ))
        # Should have escalated to tier 4
        assert result.trace.tier_used == 4
        assert "agentic_reformulation" in result.trace.strategies_used

    @pytest.mark.asyncio
    async def test_tier3_insufficient_but_max_tier_3_returns(self):
        """When tier 3 is insufficient but max_tier=3, return what we have."""
        pipeline, vs, _ = _make_pipeline()
        # Only one memory — less than min_results=5
        await _seed_vectors(pipeline, ["Alice works at NASA"])

        retriever = TieredRetriever(pipeline, min_results=5, max_tier=3)
        result = await retriever.retrieve(RecallRequest(
            query="NASA", bank_id="b1", max_results=10,
        ))
        assert result.trace.tier_used == 3  # Didn't escalate further
        assert len(result.hits) >= 1  # Still returned what it found


# ---------------------------------------------------------------------------
# Tier 4: Agentic reformulation
# ---------------------------------------------------------------------------


class TestTier4Agentic:
    @pytest.mark.asyncio
    async def test_tier4_reformulates_and_retries(self):
        """Tier 4 reformulates query via LLM, then runs tier 3 again."""
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA on rocket propulsion"])

        retriever = TieredRetriever(pipeline, min_results=99, max_tier=4)
        result = await retriever.retrieve(RecallRequest(
            query="rockets", bank_id="b1", max_results=5,
        ))
        assert result.trace.tier_used == 4
        assert "agentic_reformulation" in result.trace.strategies_used

    @pytest.mark.asyncio
    async def test_tier4_preserves_request_fields(self):
        """Tier 4 reformulated request should carry original tags, filters, etc."""
        pipeline, vs, _ = _make_pipeline()

        captured_requests: list[RecallRequest] = []

        async def capturing_recall(req: RecallRequest) -> RecallResult:
            captured_requests.append(req)
            return RecallResult(
                hits=[], total_available=0, truncated=False,
                trace=RecallTrace(strategies_used=[], total_candidates=0, fusion_method="rrf"),
            )

        retriever = TieredRetriever(
            pipeline, min_results=99, max_tier=4, full_recall=capturing_recall,
        )
        await retriever.retrieve(RecallRequest(
            query="rockets", bank_id="b1", max_results=5,
            tags=["science"], fact_types=["world"],
        ))

        # Tier 3 call + tier 4 reformulated call
        assert len(captured_requests) == 2
        reformulated_req = captured_requests[1]
        assert reformulated_req.tags == ["science"]
        assert reformulated_req.fact_types == ["world"]
        assert reformulated_req.bank_id == "b1"
        assert reformulated_req.max_results == 5
        # Query should be different (reformulated)
        assert reformulated_req.query != ""

    @pytest.mark.asyncio
    async def test_tier4_caches_result(self):
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA"])
        cache = RecallCache(similarity_threshold=0.8)

        retriever = TieredRetriever(
            pipeline, recall_cache=cache, min_results=99, max_tier=4,
        )
        await retriever.retrieve(RecallRequest(query="rockets", bank_id="b1", max_results=5))
        assert cache.size("b1") >= 1


# ---------------------------------------------------------------------------
# Reformulation
# ---------------------------------------------------------------------------


class TestReformulateQuery:
    @pytest.mark.asyncio
    async def test_reformulation_returns_string(self):
        pipeline, _, _ = _make_pipeline()
        retriever = TieredRetriever(pipeline, max_tier=4)
        result = await retriever._reformulate_query("What did Alice do?")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_reformulation_truncates_long_query(self):
        """Queries over 2000 chars are truncated before sending to LLM."""
        pipeline, _, _ = _make_pipeline()
        retriever = TieredRetriever(pipeline, max_tier=4)
        long_query = "x" * 5000
        result = await retriever._reformulate_query(long_query)
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_reformulation_fallback_on_error(self):
        """If LLM fails, return original query."""
        pipeline, _, _ = _make_pipeline()

        # Replace LLM with one that always fails
        class FailingLLM:
            SPI_VERSION = 1
            async def complete(self, *a, **kw):
                raise RuntimeError("LLM unavailable")
            async def embed(self, texts, **kw):
                return [[0.0] * 128 for _ in texts]

        pipeline.llm_provider = FailingLLM()
        retriever = TieredRetriever(pipeline, max_tier=4)
        result = await retriever._reformulate_query("original query")
        assert result == "original query"


# ---------------------------------------------------------------------------
# max_tier boundary behavior
# ---------------------------------------------------------------------------


class TestMaxTier:
    @pytest.mark.asyncio
    async def test_max_tier_0_only_cache(self):
        """max_tier=0 should only try cache, then return empty."""
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA"])

        retriever = TieredRetriever(pipeline, recall_cache=RecallCache(), max_tier=0)
        result = await retriever.retrieve(RecallRequest(
            query="NASA", bank_id="b1", max_results=5,
        ))
        # No cache entry exists — should return empty (no escalation)
        assert result.hits == []
        assert result.trace.tier_used == 0

    @pytest.mark.asyncio
    async def test_max_tier_capped_at_4(self):
        """max_tier > 4 should be silently capped to 4."""
        pipeline, _, _ = _make_pipeline()
        retriever = TieredRetriever(pipeline, max_tier=10)
        assert retriever.max_tier == 4

    @pytest.mark.asyncio
    async def test_max_tier_2_skips_full_recall(self):
        """max_tier=2 with no doc store should return empty (can't do BM25 or full)."""
        pipeline, vs, _ = _make_pipeline(with_doc_store=False)
        await _seed_vectors(pipeline, ["Alice works at NASA"])

        retriever = TieredRetriever(pipeline, max_tier=2)
        result = await retriever.retrieve(RecallRequest(
            query="NASA", bank_id="b1", max_results=5,
        ))
        # No doc store for tier 2, no tier 3 allowed → empty
        assert result.hits == []


# ---------------------------------------------------------------------------
# Fallback: empty result with external context
# ---------------------------------------------------------------------------


class TestFallbackExternalContext:
    @pytest.mark.asyncio
    async def test_empty_local_with_external_context(self):
        """When all tiers fail but external_context exists, return external hits."""
        pipeline, _, _ = _make_pipeline()

        ext = [
            MemoryHit(text="External fact A", score=0.9),
            MemoryHit(text="External fact B", score=0.7),
        ]
        retriever = TieredRetriever(pipeline, max_tier=0)  # Only cache, no cache entry
        result = await retriever.retrieve(RecallRequest(
            query="anything", bank_id="b1", max_results=5, external_context=ext,
        ))
        assert len(result.hits) == 2
        assert result.hits[0].text == "External fact A"

    @pytest.mark.asyncio
    async def test_empty_local_no_external(self):
        """When all tiers fail and no external context, return genuinely empty."""
        pipeline, _, _ = _make_pipeline()

        retriever = TieredRetriever(pipeline, max_tier=0)
        result = await retriever.retrieve(RecallRequest(
            query="anything", bank_id="b1", max_results=5,
        ))
        assert result.hits == []
        assert result.total_available == 0


# ---------------------------------------------------------------------------
# Cross-bank isolation
# ---------------------------------------------------------------------------


class TestCrossBankIsolation:
    @pytest.mark.asyncio
    async def test_cache_is_per_bank(self):
        """Cache entries for bank A should not be returned for bank B."""
        pipeline, vs, _ = _make_pipeline()
        await _seed_vectors(pipeline, ["Alice works at NASA"], bank="bank-a")
        cache = RecallCache(similarity_threshold=0.8)

        retriever = TieredRetriever(pipeline, recall_cache=cache, max_tier=3)

        # Populate cache for bank-a
        r1 = await retriever.retrieve(RecallRequest(query="NASA", bank_id="bank-a", max_results=5))
        assert len(r1.hits) >= 1
        assert cache.size("bank-a") == 1

        # Query bank-b — should not get bank-a's cached result
        r2 = await retriever.retrieve(RecallRequest(query="NASA", bank_id="bank-b", max_results=5))
        assert r2.trace.tier_used != 0  # Not a cache hit
        assert cache.size("bank-b") == 0 or r2.hits == []
