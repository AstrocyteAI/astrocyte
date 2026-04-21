"""Pipeline orchestrator — coordinates Tier 1 retain/recall/reflect flows.

Async (coordinates I/O stages). See docs/_design/built-in-pipeline.md.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from astrocyte.config import ExtractionProfileConfig, RecallAuthorityConfig
from astrocyte.pipeline.chunking import DEFAULT_CHUNK_SIZE, chunk_text
from astrocyte.pipeline.embedding import generate_embeddings
from astrocyte.pipeline.entity_extraction import extract_entities
from astrocyte.pipeline.extraction import (
    merged_user_and_builtin_profiles,
    prepare_retain_input,
    resolve_retain_chunking,
)
from astrocyte.pipeline.fusion import (
    DEFAULT_RRF_K,
    memory_hits_as_scored,
    rrf_fusion,
    weighted_rrf_fusion,
)
from astrocyte.pipeline.query_intent import (
    QueryIntent,
    classify_query_intent,
    weights_for_intent,
)
from astrocyte.pipeline.reflect import synthesize
from astrocyte.pipeline.reranking import basic_rerank
from astrocyte.pipeline.retrieval import parallel_retrieve
from astrocyte.policy.homeostasis import enforce_token_budget
from astrocyte.policy.signal_quality import DedupDetector
from astrocyte.recall.authority import apply_recall_authority, merge_retain_metadata_authority_tier
from astrocyte.types import (
    Completion,
    EntityLink,
    MemoryEntityAssociation,
    MemoryHit,
    Message,
    RecallRequest,
    RecallResult,
    RecallTrace,
    ReflectRequest,
    ReflectResult,
    RetainRequest,
    RetainResult,
    VectorFilters,
    VectorItem,
)

if TYPE_CHECKING:
    from astrocyte.mip.router import MipRouter
    from astrocyte.mip.schema import PipelineSpec
    from astrocyte.provider import DocumentStore, GraphStore, LLMProvider, VectorStore


_logger = logging.getLogger("astrocyte.mip")


def _warn_on_version_drift(
    bank_pipeline: PipelineSpec | None,
    hits: list[MemoryHit],
    bank_id: str,
) -> None:
    """Emit a single warning when retrieved hits were retained under a different MIP version.

    Soft signal — does not affect recall results. Hits without a persisted
    ``_mip.pipeline_version`` are ignored (they were retained before MIP, by a
    rule with no version, or by a different rule).
    """
    if bank_pipeline is None or bank_pipeline.version is None:
        return
    current_version = int(bank_pipeline.version)
    seen: set[int] = set()
    for hit in hits:
        if not hit.metadata:
            continue
        v = hit.metadata.get("_mip.pipeline_version")
        if v is None:
            continue
        try:
            v_int = int(v)
        except (TypeError, ValueError):
            continue
        if v_int != current_version:
            seen.add(v_int)
    if seen:
        _logger.warning(
            "MIP pipeline version drift in bank %r: current=%d, hits retained under versions %s. "
            "Consider re-indexing or accepting the drift.",
            bank_id, current_version, sorted(seen),
        )


class _TrackingLLMProvider:
    """Transparent wrapper that accumulates token usage from an LLMProvider.

    All calls are forwarded to the underlying provider. After each ``complete()``
    call, ``Completion.usage`` (if present) is added to ``tokens_used``.
    Embedding calls do not consume completion tokens and are forwarded as-is.

    This is used internally by :class:`PipelineOrchestrator` so that the
    evaluation framework can report ``total_tokens_used`` per run without
    modifying individual pipeline modules. See ``evaluation.md`` §2.2.
    """

    SPI_VERSION: int = 1

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.tokens_used: int = 0

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        result = await self._inner.complete(messages, model=model, max_tokens=max_tokens, temperature=temperature)
        if result.usage:
            self.tokens_used += result.usage.input_tokens + result.usage.output_tokens
        return result

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        return await self._inner.embed(texts, model=model)

    def capabilities(self) -> Any:
        return self._inner.capabilities()

    def reset_tokens(self) -> int:
        """Return accumulated tokens and reset counter to zero."""
        total = self.tokens_used
        self.tokens_used = 0
        return total


class PipelineOrchestrator:
    """Orchestrates the Tier 1 built-in intelligence pipeline.

    Coordinates async stages: chunk → embed → store → retrieve → fuse → rerank.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        llm_provider: LLMProvider,
        graph_store: GraphStore | None = None,
        document_store: DocumentStore | None = None,
        chunk_strategy: str = "sentence",
        max_chunk_size: int = DEFAULT_CHUNK_SIZE,
        rrf_k: int = DEFAULT_RRF_K,
        semantic_overfetch: int = 5,
        extraction_profiles: dict[str, ExtractionProfileConfig] | None = None,
        *,
        enable_temporal_retrieval: bool = True,
        temporal_scan_cap: int = 500,
        temporal_half_life_days: float = 7.0,
        enable_intent_aware_recall: bool = True,
    ) -> None:
        self.vector_store = vector_store
        self._tracker = _TrackingLLMProvider(llm_provider)
        self.llm_provider: LLMProvider = self._tracker  # type: ignore[assignment]
        self.graph_store = graph_store
        self.document_store = document_store
        self.chunk_strategy = chunk_strategy
        self.max_chunk_size = max_chunk_size
        self.extraction_profiles = extraction_profiles
        self.rrf_k = rrf_k
        self.semantic_overfetch = semantic_overfetch
        # Temporal retrieval knobs — see astrocyte.pipeline.retrieval for full
        # semantics. Enabled by default: the strategy no-ops gracefully on
        # banks whose vectors have no timestamps, so enabling it is safe.
        self.enable_temporal_retrieval = enable_temporal_retrieval
        self.temporal_scan_cap = temporal_scan_cap
        self.temporal_half_life_days = temporal_half_life_days
        # Intent-aware recall: heuristic query classifier biases RRF
        # weights per strategy. Conservative (always fuses all strategies
        # even under bias), so enabling is safe — a misclassification
        # degrades to a soft lean rather than a strategy drop.
        self.enable_intent_aware_recall = enable_intent_aware_recall
        self._dedup = DedupDetector(similarity_threshold=0.95)
        #: Set by :meth:`astrocyte._astrocyte.Astrocyte.set_pipeline` when ``recall_authority`` is configured.
        self.recall_authority: RecallAuthorityConfig | None = None
        #: Set by :meth:`astrocyte._astrocyte.Astrocyte.set_pipeline` when MIP is configured.
        #: Used by :meth:`recall` to resolve per-bank rerank/reflect overrides (P3).
        self.mip_router: MipRouter | None = None

    @property
    def tokens_used(self) -> int:
        """Total LLM tokens consumed through this orchestrator since last reset."""
        return self._tracker.tokens_used

    def reset_token_counter(self) -> int:
        """Return accumulated token count and reset to zero."""
        return self._tracker.reset_tokens()

    async def retain(self, request: RetainRequest) -> RetainResult:
        """Retain pipeline: normalize → chunk → extract entities → embed → store."""
        # 0–1. Raw → normalizer → profile metadata/tags (M3 extraction chain)
        profile: ExtractionProfileConfig | None = None
        name = getattr(request, "extraction_profile", None)
        profile_table = merged_user_and_builtin_profiles(self.extraction_profiles)
        if name:
            profile = profile_table.get(name)

        # MIP pipeline overrides (from RoutingDecision) — optional, no behavior
        # change when absent. ``chunker`` and ``dedup`` are consumed here;
        # ``rerank`` and ``reflect`` apply to recall/reflect (Phase 2).
        mip_pipeline = request.mip_pipeline
        mip_chunker = mip_pipeline.chunker if mip_pipeline else None
        mip_dedup = mip_pipeline.dedup if mip_pipeline else None
        dedup_threshold_override = mip_dedup.threshold if mip_dedup else None
        dedup_action = (mip_dedup.action if mip_dedup else None) or "skip_chunk"

        prepared = prepare_retain_input(
            request,
            profile,
            graph_store_configured=self.graph_store is not None,
        )
        chunking = resolve_retain_chunking(
            prepared.effective_content_type,
            profile=profile,
            default_strategy=self.chunk_strategy,
            default_max_chunk_size=self.max_chunk_size,
            mip_chunker=mip_chunker,
        )
        chunk_kwargs: dict[str, int] = {"max_chunk_size": chunking.max_size}
        if chunking.overlap is not None:
            chunk_kwargs["overlap"] = chunking.overlap
        chunks = chunk_text(prepared.text, strategy=chunking.strategy, **chunk_kwargs)
        if not chunks:
            return RetainResult(stored=False, error="No content after chunking")

        # 2. Generate embeddings for all chunks
        embeddings = await generate_embeddings(chunks, self.llm_provider)

        # 2b. Per-chunk dedup — behavior depends on dedup_action:
        #     "skip_chunk" (default): drop duplicate chunks, keep the rest
        #     "skip":     if any chunk is a duplicate, reject the entire retain
        #     "warn":     keep all chunks regardless of duplicates
        #     "update":   not yet implemented; falls back to "skip_chunk"
        keep_indices: list[int] = []
        any_duplicate = False
        for i, emb in enumerate(embeddings):
            is_dup, _sim = self._dedup.is_duplicate(
                request.bank_id, emb, threshold_override=dedup_threshold_override,
            )
            if is_dup:
                any_duplicate = True
            if dedup_action == "warn" or not is_dup:
                keep_indices.append(i)

        if dedup_action == "skip" and any_duplicate:
            return RetainResult(stored=False, deduplicated=True, error="Duplicate chunk(s) found; rule action=skip")

        if not keep_indices:
            return RetainResult(stored=False, deduplicated=True, error="All chunks are near-duplicates")

        chunks = [chunks[i] for i in keep_indices]
        embeddings = [embeddings[i] for i in keep_indices]

        # 3. Extract entities (profile can disable; requires graph store)
        entities = await extract_entities(prepared.text, self.llm_provider) if prepared.extract_entities else []

        profile_tier = profile.authority_tier if profile else None
        chunk_metadata = merge_retain_metadata_authority_tier(
            prepared.metadata,
            bank_id=request.bank_id,
            profile_authority_tier=profile_tier,
            recall_authority=self.recall_authority,
        )

        # Persist MIP provenance so recall can warn on rule-version drift (P2).
        if request.mip_rule_name or (mip_pipeline and mip_pipeline.version is not None):
            chunk_metadata = dict(chunk_metadata or {})
            if request.mip_rule_name:
                chunk_metadata["_mip.rule"] = request.mip_rule_name
            if mip_pipeline and mip_pipeline.version is not None:
                chunk_metadata["_mip.pipeline_version"] = int(mip_pipeline.version)

        # Stamp ``_created_at`` so (1) temporal retrieval can rank by
        # recency, (2) MIP forget.min_age_days can enforce age guards, and
        # (3) lifecycle TTL has a consistent retain timestamp. Callers that
        # already set ``_created_at`` (imports / replay) win via setdefault.
        # Mirrors InMemoryEngineProvider.retain so pipeline- and engine-
        # backed deployments share the same observability contract.
        chunk_metadata = dict(chunk_metadata or {})
        chunk_metadata.setdefault(
            "_created_at", datetime.now(timezone.utc).isoformat(),
        )

        # 4. Store vectors
        memory_ids: list[str] = []
        items: list[VectorItem] = []
        for chunk, embedding in zip(chunks, embeddings):
            mem_id = uuid.uuid4().hex[:16]
            memory_ids.append(mem_id)
            items.append(
                VectorItem(
                    id=mem_id,
                    bank_id=request.bank_id,
                    vector=embedding,
                    text=chunk,
                    metadata=chunk_metadata,
                    tags=prepared.tags,
                    fact_type=prepared.fact_type,
                    occurred_at=request.occurred_at,
                )
            )

        await self.vector_store.store_vectors(items)

        # 4b. Mirror chunks into the document store so keyword (BM25) search
        # has content to retrieve from. Previously recall.parallel_retrieve
        # ran a keyword strategy when ``document_store`` was configured —
        # but nothing was ever stored there. Fixed here: every chunk that
        # lands in the vector store also lands in the document store, with
        # the same memory_id so RRF fusion can dedupe across strategies.
        # Failures don't abort the retain — degrading to semantic-only
        # retrieval is preferable to losing the whole memory.
        if self.document_store is not None:
            from astrocyte.types import Document

            for item in items:
                try:
                    await self.document_store.store_document(
                        Document(
                            id=item.id,
                            text=item.text,
                            metadata=item.metadata,
                            tags=item.tags,
                        ),
                        request.bank_id,
                    )
                except Exception as exc:
                    _logger.warning(
                        "document_store.store_document failed for chunk %s: %s "
                        "(keyword retrieval will miss this chunk)",
                        item.id, exc,
                    )

        # 5. Store entities and links (if graph store configured)
        if self.graph_store and entities:
            entity_ids = await self.graph_store.store_entities(entities, request.bank_id)

            # Link memories to entities
            associations = [
                MemoryEntityAssociation(memory_id=mid, entity_id=eid) for mid in memory_ids for eid in entity_ids
            ]
            await self.graph_store.link_memories_to_entities(associations, request.bank_id)

            # Create co-occurrence links between entities
            if len(entity_ids) > 1:
                links = [
                    EntityLink(
                        source_entity_id=entity_ids[i],
                        target_entity_id=entity_ids[j],
                        link_type="co_occurs",
                    )
                    for i in range(len(entity_ids))
                    for j in range(i + 1, len(entity_ids))
                ]
                await self.graph_store.store_links(links, request.bank_id)

        # 6. Update dedup cache with stored embeddings
        for mem_id, emb in zip(memory_ids, embeddings):
            self._dedup.add(request.bank_id, mem_id, emb)

        return RetainResult(
            stored=True,
            memory_id=memory_ids[0] if memory_ids else None,
        )

    async def recall(self, request: RecallRequest) -> RecallResult:
        """Recall pipeline: embed query → parallel retrieve → fuse → rerank → budget."""
        # 1. Embed query
        query_embeddings = await generate_embeddings([request.query], self.llm_provider)
        query_vector = query_embeddings[0]

        # 2. Build filters
        filters = VectorFilters(
            bank_id=request.bank_id,
            tags=request.tags,
            fact_types=request.fact_types,
            time_range=request.time_range,
        )

        # 2b. Extract entities from query for graph search
        entity_ids: list[str] | None = None
        if self.graph_store:
            query_entities = await extract_entities(request.query, self.llm_provider)
            if query_entities:
                # Look up entity IDs in the graph store
                found: list[str] = []
                for ent in query_entities:
                    matches = await self.graph_store.query_entities(ent.name, request.bank_id, limit=3)
                    found.extend(m.id for m in matches)
                entity_ids = found or None

        # Resolve per-bank MIP PipelineSpec once (reused for temporal
        # half-life override at retrieval time AND for rerank + version
        # check further down). Previously resolved after retrieval — moved
        # earlier so temporal_half_life_days can flow into parallel_retrieve.
        bank_pipeline = None
        if self.mip_router is not None:
            bank_pipeline = self.mip_router.resolve_pipeline_for_bank(request.bank_id)

        # Per-bank half-life override beats the orchestrator default.
        # Long-term knowledge banks (e.g. LongMemEval-style corpora with
        # months-old answers) set this to 90+; chat banks leave it at 7.
        effective_half_life = (
            bank_pipeline.temporal_half_life_days
            if bank_pipeline is not None and bank_pipeline.temporal_half_life_days is not None
            else self.temporal_half_life_days
        )

        # 3. Parallel retrieval
        overfetch_limit = request.max_results * self.semantic_overfetch
        strategy_results = await parallel_retrieve(
            query_vector=query_vector,
            query_text=request.query,
            bank_id=request.bank_id,
            vector_store=self.vector_store,
            graph_store=self.graph_store,
            document_store=self.document_store,
            entity_ids=entity_ids,
            limit=overfetch_limit,
            filters=filters,
            enable_temporal=self.enable_temporal_retrieval,
            temporal_scan_cap=self.temporal_scan_cap,
            temporal_half_life_days=effective_half_life,
        )

        # 4. RRF fusion (local strategies + optional federated / manual external_context)
        # When intent-aware recall is enabled, weight each strategy's RRF
        # contribution by a bias derived from the query's classified
        # intent. UNKNOWN intents pass through with neutral 1.0 weights,
        # so enabling the feature never *hurts* baseline behavior — it
        # either kicks in on a confident classification or no-ops.
        query_intent = QueryIntent.UNKNOWN
        if self.enable_intent_aware_recall:
            query_intent = classify_query_intent(request.query).intent
        intent_weights = weights_for_intent(query_intent)

        weighted_inputs: list[tuple[list[Any], float]] = []
        for strategy, results in strategy_results.items():
            if not results:
                continue
            # Map strategy name → weight. External/proxy results use
            # semantic weight (they're typically semantic fusions upstream).
            weight = getattr(intent_weights, strategy, 1.0)
            weighted_inputs.append((results, weight))
        if request.external_context:
            weighted_inputs.append(
                (memory_hits_as_scored(request.external_context), intent_weights.semantic),
            )

        # Short-circuit to plain RRF when all weights are 1.0 — keeps the
        # baseline path bit-identical for deployments that disable
        # intent-aware recall.
        if all(w == 1.0 for _, w in weighted_inputs):
            fused = rrf_fusion([r for r, _ in weighted_inputs], k=self.rrf_k)
        else:
            fused = weighted_rrf_fusion(weighted_inputs, k=self.rrf_k)

        # 5. Reranking — apply per-bank MIP RerankSpec when a rule targets this bank (P3)
        # (bank_pipeline already resolved above for temporal half-life override)
        mip_rerank = bank_pipeline.rerank if bank_pipeline is not None else None
        reranked = basic_rerank(fused, request.query, mip_rerank=mip_rerank)

        # 6. Trim to max_results
        trimmed = reranked[: request.max_results]

        # 7. Convert to MemoryHit
        hits = [
            MemoryHit(
                text=item.text,
                score=item.score,
                fact_type=item.fact_type,
                metadata=item.metadata,
                tags=item.tags,
                memory_id=item.id,
                bank_id=request.bank_id,
            )
            for item in trimmed
        ]

        # 7b. Version-drift warning (Phase 2, Step 10)
        # Compare each hit's persisted ``_mip.pipeline_version`` against the
        # version currently configured for this bank's rule. Stale hits indicate
        # rule changes since retain — operator should re-index or accept drift.
        _warn_on_version_drift(bank_pipeline, hits, request.bank_id)

        # 8. Token budget
        truncated = False
        if request.max_tokens:
            hits, truncated = enforce_token_budget(hits, request.max_tokens)

        ext_n = len(request.external_context) if request.external_context else 0
        strategies_used = list(strategy_results.keys())
        if ext_n:
            strategies_used.append("proxy")
        total_candidates = sum(len(r) for r in strategy_results.values()) + ext_n

        return RecallResult(
            hits=hits,
            total_available=len(fused),
            truncated=truncated,
            trace=RecallTrace(
                strategies_used=strategies_used,
                total_candidates=total_candidates,
                fusion_method="rrf",
            ),
        )

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        """Reflect pipeline: recall → LLM synthesis."""
        # 1. Run recall with larger result set
        recall_request = RecallRequest(
            query=request.query,
            bank_id=request.bank_id,
            max_results=30,  # Larger set for synthesis context
            max_tokens=request.max_tokens,
        )
        recall_result = await self.recall(recall_request)

        auth_ctx: str | None = None
        ra = self.recall_authority
        if ra and ra.enabled and ra.apply_to_reflect:
            recall_result = apply_recall_authority(recall_result, ra)
            auth_ctx = recall_result.authority_context

        # 2. Resolve per-bank ReflectSpec from MIP (Phase 2, Step 9)
        mip_reflect = None
        if self.mip_router is not None:
            bank_pipeline = self.mip_router.resolve_pipeline_for_bank(request.bank_id)
            if bank_pipeline is not None:
                mip_reflect = bank_pipeline.reflect

        # 3. Synthesize via LLM
        return await synthesize(
            query=request.query,
            hits=recall_result.hits,
            llm_provider=self.llm_provider,
            dispositions=request.dispositions,
            max_tokens=request.max_tokens or 2048,
            authority_context=auth_ctx,
            mip_reflect=mip_reflect,
        )
