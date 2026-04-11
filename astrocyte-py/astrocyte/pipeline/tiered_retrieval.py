"""Adaptive tiered retrieval — progressive escalation from cache to agentic recall.

Inspired by ByteRover's 5-tier approach: most queries resolve cheaply.
Only novel/ambiguous queries escalate to full multi-strategy retrieval.

Sync decision logic, async execution — Rust migration candidate for the sync parts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from astrocyte.pipeline.recall_cache import RecallCache
from astrocyte.recall.merge_result import merge_external_into_recall_result
from astrocyte.types import MemoryHit, RecallRequest, RecallResult, RecallTrace

if TYPE_CHECKING:
    from astrocyte.pipeline.orchestrator import PipelineOrchestrator

# Escalation target for tier 3+ (and tier 4 post-reformulation). Default: built-in pipeline recall.
FullRecallFn = Callable[[RecallRequest], Awaitable[RecallResult]]


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
        *,
        full_recall: FullRecallFn | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.recall_cache = recall_cache
        self.min_results = min_results
        self.min_score = min_score
        self.max_tier = min(max_tier, 4)
        # When None, tier 3+ uses ``pipeline.recall`` (pipeline-only tiering).
        self._full_recall: FullRecallFn | None = full_recall

    async def _invoke_full_recall(self, request: RecallRequest) -> RecallResult:
        if self._full_recall is not None:
            return await self._full_recall(request)
        return await self.pipeline.recall(request)

    async def retrieve(self, request: RecallRequest) -> RecallResult:
        """Run tiered retrieval, escalating until sufficient results found."""
        from astrocyte.pipeline.embedding import generate_embeddings

        # Federated / proxy hits must not use stale cache entries and are not cached themselves.
        allow_cache = not request.external_context

        # ── Tier 0: Cache ──
        if self.recall_cache and self.max_tier >= 0 and allow_cache:
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
                    if request.external_context:
                        result = merge_external_into_recall_result(
                            result, request.external_context, request.max_results
                        )
                    # Cache the result for future queries
                    if self.recall_cache and query_vector and allow_cache:
                        self.recall_cache.put(request.bank_id, query_vector, result)
                    return result

        # ── Tier 3: Full multi-strategy (pipeline recall or injected full recall, e.g. hybrid) ──
        if self.max_tier >= 3:
            result = await self._invoke_full_recall(request)
            # Tag the trace
            if result.trace:
                result.trace.tier_used = 3
                result.trace.cache_hit = False

            # Check if sufficient
            if len(result.hits) >= self.min_results or self.max_tier <= 3:
                # Cache the result
                if self.recall_cache and query_vector and allow_cache:
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
                time_range=request.time_range,
                include_sources=request.include_sources,
                layer_weights=request.layer_weights,
                detail_level=request.detail_level,
                external_context=request.external_context,
            )
            result = await self._invoke_full_recall(reformulated_request)
            if result.trace:
                result.trace.tier_used = 4
                result.trace.cache_hit = False
                result.trace.strategies_used = (result.trace.strategies_used or []) + ["agentic_reformulation"]

            if self.recall_cache and query_vector and allow_cache:
                self.recall_cache.put(request.bank_id, query_vector, result)
            return result

        # Fallback: empty result (still surface federated hits if present)
        empty = RecallResult(
            hits=[],
            total_available=0,
            truncated=False,
            trace=RecallTrace(tier_used=0, cache_hit=False),
        )
        if request.external_context:
            return merge_external_into_recall_result(empty, request.external_context, request.max_results)
        return empty

    async def _reformulate_query(self, query: str) -> str:
        """Use LLM to reformulate an ambiguous query for better retrieval."""
        from astrocyte.types import Message

        system_msg = (
            "Reformulate the user's query to improve memory search results. "
            "Add synonyms, expand abbreviations, and rephrase for clarity. "
            "Return only the reformulated query, nothing else."
        )
        user_msg = f"<query>\n{query[:2000]}\n</query>"
        try:
            completion = await self.pipeline.llm_provider.complete(
                messages=[
                    Message(role="system", content=system_msg),
                    Message(role="user", content=user_msg),
                ],
                max_tokens=200,
                temperature=0.3,
            )
            return completion.text.strip() or query
        except Exception:
            return query  # Fallback to original
