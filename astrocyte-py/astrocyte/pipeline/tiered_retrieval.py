"""Adaptive tiered retrieval — progressive escalation from cache to agentic recall.

Inspired by ByteRover's 5-tier approach: most queries resolve cheaply.
Only novel/ambiguous queries escalate to full multi-strategy retrieval.

Sync decision logic, async execution — Rust migration candidate for the sync parts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrocyte.pipeline.recall_cache import RecallCache
from astrocyte.types import MemoryHit, RecallRequest, RecallResult, RecallTrace

if TYPE_CHECKING:
    from astrocyte.pipeline.orchestrator import PipelineOrchestrator


class TieredRetriever:
    """5-tier progressive retrieval — escalates only when needed.

    Tier 0: Recall cache hit (~0ms, free)
    Tier 1: Fuzzy text match on recent memories (~5ms, free)
    Tier 2: BM25 keyword search only (~50ms, free)
    Tier 3: Full multi-strategy parallel retrieval (~200ms, embedding cost)
    Tier 4: Agentic recall — LLM reformulates query + retry (~3-10s, LLM cost)
    """

    def __init__(
        self,
        pipeline: PipelineOrchestrator,
        recall_cache: RecallCache | None = None,
        min_results: int = 3,
        min_score: float = 0.3,
        max_tier: int = 3,
    ) -> None:
        self.pipeline = pipeline
        self.recall_cache = recall_cache
        self.min_results = min_results
        self.min_score = min_score
        self.max_tier = min(max_tier, 4)

    async def retrieve(self, request: RecallRequest) -> RecallResult:
        """Run tiered retrieval, escalating until sufficient results found."""
        from astrocyte.pipeline.embedding import generate_embeddings

        # ── Tier 0: Cache ──
        if self.recall_cache and self.max_tier >= 0:
            query_embeddings = await generate_embeddings([request.query], self.pipeline.llm_provider)
            query_vector = query_embeddings[0]

            cached = self.recall_cache.get(request.bank_id, query_vector)
            if cached is not None:
                return RecallResult(
                    hits=cached.hits[: request.max_results],
                    total_available=cached.total_available,
                    truncated=cached.truncated,
                    trace=RecallTrace(
                        strategies_used=["cache"],
                        total_candidates=len(cached.hits),
                        fusion_method="cache",
                        tier_used=0,
                        cache_hit=True,
                    ),
                )
        else:
            query_vector = None

        # ── Tier 2: BM25 keyword only ──
        if self.pipeline.document_store and self.max_tier >= 2:
            keyword_hits = await self.pipeline.document_store.search_fulltext(
                request.query, request.bank_id, limit=request.max_results * 2
            )
            if len(keyword_hits) >= self.min_results:
                avg_score = sum(h.score for h in keyword_hits) / max(len(keyword_hits), 1)
                if avg_score >= self.min_score:
                    hits = [
                        MemoryHit(
                            text=h.text,
                            score=h.score,
                            metadata=h.metadata,
                            memory_id=h.document_id,
                            bank_id=request.bank_id,
                        )
                        for h in keyword_hits[: request.max_results]
                    ]
                    result = RecallResult(
                        hits=hits,
                        total_available=len(keyword_hits),
                        truncated=len(keyword_hits) > request.max_results,
                        trace=RecallTrace(
                            strategies_used=["keyword"],
                            total_candidates=len(keyword_hits),
                            fusion_method="bm25_only",
                            tier_used=2,
                            cache_hit=False,
                        ),
                    )
                    # Cache the result for future queries
                    if self.recall_cache and query_vector:
                        self.recall_cache.put(request.bank_id, query_vector, result)
                    return result

        # ── Tier 3: Full multi-strategy (delegate to pipeline's standard recall) ──
        if self.max_tier >= 3:
            result = await self.pipeline.recall(request)
            # Tag the trace
            if result.trace:
                result.trace.tier_used = 3
                result.trace.cache_hit = False

            # Check if sufficient
            if len(result.hits) >= self.min_results or self.max_tier <= 3:
                # Cache the result
                if self.recall_cache and query_vector:
                    self.recall_cache.put(request.bank_id, query_vector, result)
                return result

        # ── Tier 4: Agentic recall — LLM reformulates query ──
        if self.max_tier >= 4:
            reformulated = await self._reformulate_query(request.query)
            reformulated_request = RecallRequest(
                query=reformulated,
                bank_id=request.bank_id,
                max_results=request.max_results,
                max_tokens=request.max_tokens,
                tags=request.tags,
                fact_types=request.fact_types,
            )
            result = await self.pipeline.recall(reformulated_request)
            if result.trace:
                result.trace.tier_used = 4
                result.trace.cache_hit = False
                result.trace.strategies_used = (result.trace.strategies_used or []) + ["agentic_reformulation"]

            if self.recall_cache and query_vector:
                self.recall_cache.put(request.bank_id, query_vector, result)
            return result

        # Fallback: empty result
        return RecallResult(
            hits=[],
            total_available=0,
            truncated=False,
            trace=RecallTrace(tier_used=0, cache_hit=False),
        )

    async def _reformulate_query(self, query: str) -> str:
        """Use LLM to reformulate an ambiguous query for better retrieval."""
        from astrocyte.types import Message

        prompt = (
            "Reformulate this query to improve memory search results. "
            "Add synonyms, expand abbreviations, and rephrase for clarity. "
            "Return only the reformulated query, nothing else.\n\n"
            f"Original query: {query}"
        )
        try:
            completion = await self.pipeline.llm_provider.complete(
                messages=[Message(role="user", content=prompt)],
                max_tokens=200,
                temperature=0.3,
            )
            return completion.text.strip() or query
        except Exception:
            return query  # Fallback to original
