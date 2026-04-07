"""Hybrid Tier-2 engine + Tier-1 pipeline as a single :class:`~astrocyte.provider.EngineProvider`.

Both backends answer the same ``bank_id``; recall merges ranked hits (weighted, deduped).
Retain writes to exactly one backend via ``retain_target``.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from astrocyte.policy.homeostasis import enforce_token_budget

if TYPE_CHECKING:
    from astrocyte.pipeline.orchestrator import PipelineOrchestrator
    from astrocyte.provider import EngineProvider

from astrocyte.types import (
    EngineCapabilities,
    ForgetRequest,
    ForgetResult,
    HealthStatus,
    MemoryHit,
    RecallRequest,
    RecallResult,
    ReflectRequest,
    ReflectResult,
    RetainRequest,
    RetainResult,
)


def _dedupe_hits_prefer_score(hits: list[MemoryHit]) -> list[MemoryHit]:
    best: dict[str, MemoryHit] = {}
    for h in hits:
        prev = best.get(h.text)
        if prev is None or h.score > prev.score:
            best[h.text] = h
    return sorted(best.values(), key=lambda x: x.score, reverse=True)


class HybridEngineProvider:
    """Merges recall from a Tier-2 ``EngineProvider`` and a Tier-1 ``PipelineOrchestrator``.

    Typical use: long-lived agent memory in the hosted engine plus local embeddings / RAG
    in the pipeline, both indexed for the same logical ``bank_id``.
    """

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        *,
        engine: EngineProvider | Any | None = None,
        pipeline: PipelineOrchestrator | Any | None = None,
        retain_target: Literal["engine", "pipeline"] = "engine",
        engine_recall_weight: float = 1.0,
        pipeline_recall_weight: float = 1.0,
        dedup_across_sources: bool = True,
        adaptive_routing: bool = False,
    ) -> None:
        if engine is None and pipeline is None:
            raise ValueError("HybridEngineProvider requires at least one of engine= or pipeline=")
        if retain_target == "engine" and engine is None:
            raise ValueError("retain_target='engine' requires engine=")
        if retain_target == "pipeline" and pipeline is None:
            raise ValueError("retain_target='pipeline' requires pipeline=")
        self._engine = engine
        self._pipeline = pipeline
        self._retain_target = retain_target
        self._engine_w = float(engine_recall_weight)
        self._pipeline_w = float(pipeline_recall_weight)
        self._dedup_across_sources = dedup_across_sources
        self._adaptive_routing = adaptive_routing
        self._router = AdaptiveRouter() if adaptive_routing else None

    def capabilities(self) -> EngineCapabilities:
        if self._engine:
            c = self._engine.capabilities()
            return EngineCapabilities(
                supports_reflect=c.supports_reflect or self._pipeline is not None,
                supports_forget=c.supports_forget or self._pipeline is not None,
                supports_semantic_search=c.supports_semantic_search or self._pipeline is not None,
                supports_keyword_search=c.supports_keyword_search,
                supports_graph_search=c.supports_graph_search,
                supports_temporal_search=c.supports_temporal_search,
                supports_dispositions=c.supports_dispositions,
                supports_consolidation=c.supports_consolidation,
                supports_entities=c.supports_entities,
                supports_tags=c.supports_tags,
                supports_metadata=c.supports_metadata,
                max_retain_bytes=c.max_retain_bytes,
                max_recall_results=c.max_recall_results,
                max_embedding_dims=c.max_embedding_dims,
            )
        return EngineCapabilities(
            supports_reflect=True,
            supports_forget=self._pipeline is not None,
            supports_semantic_search=True,
            supports_tags=True,
            supports_metadata=True,
        )

    async def health(self) -> HealthStatus:
        parts: list[str] = []
        healthy = True
        if self._engine:
            h = await self._engine.health()
            healthy = healthy and h.healthy
            parts.append(f"engine={h.message or h.healthy}")
        if self._pipeline:
            h = await self._pipeline.vector_store.health()
            healthy = healthy and h.healthy
            parts.append(f"pipeline={h.message or h.healthy}")
        return HealthStatus(healthy=healthy, message="; ".join(parts))

    async def retain(self, request: RetainRequest) -> RetainResult:
        if self._retain_target == "engine":
            return await self._engine.retain(request)
        return await self._pipeline.retain(request)

    async def recall(self, request: RecallRequest) -> RecallResult:
        # Adaptive routing: compute per-query weights
        engine_w = self._engine_w
        pipeline_w = self._pipeline_w
        if self._router and self._engine:
            caps = self._engine.capabilities()
            engine_w, pipeline_w = self._router.route(request.query, caps, self._engine_w, self._pipeline_w)

        tasks: list[Any] = []
        if self._engine:
            tasks.append(self._engine.recall(request))
        if self._pipeline:
            tasks.append(self._pipeline.recall(request))
        raw = await asyncio.gather(*tasks, return_exceptions=True)

        idx = 0
        all_hits: list[MemoryHit] = []
        total_available = 0
        if self._engine:
            eng_res = raw[idx]
            idx += 1
            if isinstance(eng_res, BaseException):
                raise eng_res
            total_available += eng_res.total_available
            for h in eng_res.hits:
                tagged = replace(h, bank_id=h.bank_id or request.bank_id)
                tagged = replace(tagged, source=tagged.source or "tier2_engine")
                all_hits.append(replace(tagged, score=tagged.score * engine_w))
        if self._pipeline:
            pipe_res = raw[idx]
            idx += 1
            if isinstance(pipe_res, BaseException):
                raise pipe_res
            total_available += pipe_res.total_available
            for h in pipe_res.hits:
                tagged = replace(h, bank_id=h.bank_id or request.bank_id)
                tagged = replace(tagged, source=tagged.source or "tier1_pipeline")
                all_hits.append(replace(tagged, score=tagged.score * pipeline_w))

        merged = (
            _dedupe_hits_prefer_score(all_hits)
            if self._dedup_across_sources
            else sorted(all_hits, key=lambda x: x.score, reverse=True)
        )
        trimmed = merged[: request.max_results]
        truncated = False
        if request.max_tokens:
            trimmed, truncated = enforce_token_budget(trimmed, request.max_tokens)
        return RecallResult(hits=trimmed, total_available=total_available, truncated=truncated)

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        if self._engine and self._engine.capabilities().supports_reflect:
            return await self._engine.reflect(request)
        if self._pipeline:
            return await self._pipeline.reflect(request)
        raise NotImplementedError("reflect is not available on this HybridEngineProvider")

    async def forget(self, request: ForgetRequest) -> ForgetResult:
        if self._engine and self._engine.capabilities().supports_forget:
            return await self._engine.forget(request)
        if self._pipeline and request.memory_ids:
            deleted = await self._pipeline.vector_store.delete(request.memory_ids, request.bank_id)
            return ForgetResult(deleted_count=deleted)
        raise NotImplementedError("forget is not available on this HybridEngineProvider")


# ---------------------------------------------------------------------------
# Adaptive query routing (Phase 2 innovation)
# ---------------------------------------------------------------------------


_TEMPORAL_PATTERNS = re.compile(
    r"\b(yesterday|today|last\s+week|last\s+month|before|after|when|recently|"
    r"\d{4}[-/]\d{2}|january|february|march|april|may|june|july|august|"
    r"september|october|november|december|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)\b",
    re.IGNORECASE,
)


class AdaptiveRouter:
    """Classifies queries and adjusts engine/pipeline weights per-query.

    Heuristic rules:
    - Temporal queries → boost engine (if it supports temporal search)
    - Entity-rich queries → boost engine (if it supports graph search)
    - Simple factual → boost pipeline (keyword/BM25 is sufficient)
    - Complex analytical → boost engine (better reflect)

    Sync, stateless — Rust migration candidate.
    """

    def route(
        self,
        query: str,
        engine_caps: EngineCapabilities | None = None,
        base_engine_weight: float = 1.0,
        base_pipeline_weight: float = 1.0,
    ) -> tuple[float, float]:
        """Compute per-query weights for engine vs pipeline.

        Returns (engine_weight, pipeline_weight).
        """
        engine_w = base_engine_weight
        pipeline_w = base_pipeline_weight

        # Temporal signals
        temporal_matches = len(_TEMPORAL_PATTERNS.findall(query))
        if temporal_matches > 0:
            if engine_caps and engine_caps.supports_temporal_search:
                engine_w *= 1.5 + 0.2 * min(temporal_matches, 3)
            else:
                pipeline_w *= 1.2  # Pipeline can still filter by date

        # Entity density — count capitalized words as proxy
        words = query.split()
        capitalized = sum(1 for w in words if w[0:1].isupper() and len(w) > 1)
        entity_ratio = capitalized / max(len(words), 1)
        if entity_ratio > 0.3:
            if engine_caps and engine_caps.supports_graph_search:
                engine_w *= 1.4
            else:
                engine_w *= 1.1

        # Question complexity — how/why suggest deeper reasoning
        lower = query.lower().strip()
        if lower.startswith(("how ", "why ", "explain ", "analyze ", "compare ")):
            if engine_caps and engine_caps.supports_reflect:
                engine_w *= 1.3
        elif lower.startswith(("what is ", "who is ", "where is ", "list ")):
            pipeline_w *= 1.2  # Simple factual → BM25 is fine

        # Short queries → pipeline (keyword sufficient)
        if len(words) <= 3:
            pipeline_w *= 1.2

        return engine_w, pipeline_w
