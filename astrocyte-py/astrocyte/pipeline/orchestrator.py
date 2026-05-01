"""Pipeline orchestrator — coordinates Tier 1 retain/recall/reflect flows.

Async (coordinates I/O stages). See docs/_design/built-in-pipeline.md.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
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
    ScoredItem,
    memory_hits_as_scored,
    rrf_fusion,
    weighted_rrf_fusion,
)
from astrocyte.pipeline.hyde import generate_hyde_vector
from astrocyte.pipeline.query_intent import (
    QueryIntent,
    classify_query_intent,
    weights_for_intent,
)
from astrocyte.pipeline.query_plan import build_query_plan
from astrocyte.pipeline.reflect import synthesize
from astrocyte.pipeline.reranking import (
    apply_context_diversity,
    basic_rerank,
    cross_encoder_like_rerank,
    llm_pairwise_rerank,
)
from astrocyte.pipeline.retrieval import parallel_retrieve
from astrocyte.policy.homeostasis import enforce_token_budget
from astrocyte.policy.signal_quality import DedupDetector
from astrocyte.recall.authority import apply_recall_authority, merge_retain_metadata_authority_tier
from astrocyte.types import (
    Completion,
    Entity,
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
    from astrocyte.pipeline.entity_resolution import EntityResolver
    from astrocyte.provider import DocumentStore, GraphStore, LLMProvider, VectorStore, WikiStore


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
            # Tier-3 observability: record cost + per-phase tokens when a
            # benchmark collector is attached. Silent no-op otherwise.
            collector = getattr(self, "_metrics_collector", None)
            if collector is not None:
                try:
                    collector.record_completion_call(
                        model=getattr(result, "model", None) or "",
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                    )
                except Exception:
                    pass  # never let metrics break a real call
        return result

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        result = await self._inner.embed(texts, model=model)
        # Embeddings don't return usage objects; estimate input tokens from
        # the rough heuristic of ~1 token per 4 chars per text (conservative).
        collector = getattr(self, "_metrics_collector", None)
        if collector is not None:
            try:
                est_tokens = sum(max(1, len(t) // 4) for t in texts)
                collector.record_embedding_call(
                    model=model or "text-embedding-3-small",
                    tokens=est_tokens,
                )
            except Exception:
                pass  # metrics are best-effort; never block the embedding call
        return result

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
        enable_multi_query_expansion: bool = True,
        enable_hyde: bool = False,
        wiki_store: WikiStore | None = None,
        wiki_confidence_threshold: float = 0.7,
        entity_resolver: EntityResolver | None = None,
        enable_observation_consolidation: bool = True,
        observation_weight: float = 0.0,
        observation_injection_weight: float = 1.5,
        multi_query_confidence_threshold: float = 0.72,
        final_rerank_mode: str = "heuristic",
        final_rerank_top_n: int = 30,
        final_rerank_keep_n: int | None = None,
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
        # Multi-query expansion: decompose complex questions into sub-questions,
        # recall for each independently, and merge via RRF. Improves multi-hop
        # coverage at the cost of N-1 extra embedding + retrieval passes per query.
        # Disabled by default; enable for multi-hop-heavy workloads.
        # When enabled, the confidence gate (multi_query_confidence_threshold)
        # suppresses expansion when the top raw semantic score already exceeds the
        # threshold — avoiding costly sub-query decomposition when a direct answer
        # is already retrieved with high confidence. Cosine similarity is used as
        # the gate signal (not RRF scores, which are rank-based and not comparable
        # across query runs). Default threshold 0.72 passes ~20-30% of queries.
        self.enable_multi_query_expansion = enable_multi_query_expansion
        self.multi_query_confidence_threshold: float = multi_query_confidence_threshold
        # HyDE (R1): generate a hypothetical answer, embed it, and run a second
        # semantic search pass with that vector.  Disabled by default — adds one
        # LLM call per recall.  Enable for multi-hop / paraphrase-heavy workloads.
        self.enable_hyde = enable_hyde
        self._dedup = DedupDetector(similarity_threshold=0.95)
        #: Set by :meth:`astrocyte._astrocyte.Astrocyte.set_pipeline` when ``recall_authority`` is configured.
        self.recall_authority: RecallAuthorityConfig | None = None
        #: Set by :meth:`astrocyte._astrocyte.Astrocyte.set_pipeline` when MIP is configured.
        #: Used by :meth:`recall` to resolve per-bank rerank/reflect overrides (P3).
        self.mip_router: MipRouter | None = None
        # M8 W5 — wiki tier precedence.  When a WikiStore is wired up and a
        # compiled wiki page scores above ``wiki_confidence_threshold``, recall
        # returns the wiki hit + raw-memory citations instead of running the full
        # parallel-retrieve / RRF pipeline.
        self.wiki_store: WikiStore | None = wiki_store
        self.wiki_confidence_threshold: float = wiki_confidence_threshold
        # M11: entity resolution — alias-of links between entities.
        # None means the stage is skipped (opt-in, no cost when disabled).
        self.entity_resolver: EntityResolver | None = entity_resolver
        # Observation consolidation — post-retain background LLM pass that
        # maintains a deduplicated observations layer.  Disabled by default
        # (opt-in).  When enabled, recall also runs a separate "observation"
        # strategy that searches the observations layer and fuses results into
        # the main RRF pipeline with a configurable weight boost.
        self.enable_observation_consolidation: bool = enable_observation_consolidation
        self.observation_weight: float = observation_weight
        # Weight applied to the ::obs bank when intent-gated injection fires
        # (EXPLORATORY / RELATIONAL queries). Kept separate from observation_weight
        # so callers can disable global injection (observation_weight=0.0) while
        # still enabling the intent-gated path.
        self.observation_injection_weight: float = observation_injection_weight
        self.final_rerank_mode = final_rerank_mode
        self.final_rerank_top_n = final_rerank_top_n
        self.final_rerank_keep_n = final_rerank_keep_n
        if enable_observation_consolidation:
            from astrocyte.pipeline.observation import ObservationConsolidator

            self._observation_consolidator: ObservationConsolidator | None = ObservationConsolidator()
        else:
            self._observation_consolidator = None
        self._background_tasks: set[asyncio.Task[None]] = set()

    @property
    def tokens_used(self) -> int:
        """Total LLM tokens consumed through this orchestrator since last reset."""
        return self._tracker.tokens_used

    def reset_token_counter(self) -> int:
        """Return accumulated token count and reset to zero."""
        return self._tracker.reset_tokens()

    async def _attach_entity_name_embeddings(self, entities: list[Entity]) -> None:
        """Embed each entity's name and attach the vector to ``entity.embedding``.

        Powers the Hindsight-inspired entity-resolution cascade — at retain
        time we generate one batched embedding call across all entity names,
        then attach the resulting vector to each entity. Both
        ``store_entities`` (for adapters that persist embeddings) and
        ``EntityResolver.resolve()`` (for the cosine-similarity tier of the
        cascade) consume the attached vectors.

        Skips entities whose ``embedding`` is already set (caller-supplied)
        and skips entities with empty/whitespace names. On failure logs a
        warning and leaves embeddings unset — the resolver degrades to
        trigram-only and the cost is correctness for that one batch, not
        a retain failure.
        """
        targets = [
            e for e in entities
            if e.embedding is None and e.name and e.name.strip()
        ]
        if not targets:
            return
        try:
            vectors = await generate_embeddings(
                [e.name.strip() for e in targets],
                self.llm_provider,
            )
        except Exception as exc:
            _logger.warning(
                "entity name embedding failed (resolver will fall back to "
                "trigram-only tier): %s", exc,
            )
            return
        for entity, vec in zip(targets, vectors, strict=False):
            entity.embedding = vec

    async def _process_record_entities(self, record: dict[str, Any]) -> None:
        """Run all entity-related I/O for a single retain_many record.

        Encapsulates the embed-names / store-entities / link-memories /
        co-occurrence-links / entity-resolution cascade so :meth:`retain_many`
        can dispatch one task per record via :func:`asyncio.gather`. Each
        record's writes target distinct ``(bank_id, id)`` and
        ``(bank_id, memory_id, entity_id)`` keys, so concurrent records
        don't contend on row-level locks.

        Failures in entity resolution are caught and logged — they must
        not abort retain because the raw memories are already stored.
        """
        if self.graph_store is None:
            return
        request: RetainRequest = record["request"]
        prepared = record["prepared"]
        memory_ids: list[str] = record["memory_ids"]
        entities: list[Entity] = record["entities"]
        if not entities:
            return

        if self.entity_resolver is not None:
            await self._attach_entity_name_embeddings(entities)
            # Path B (Hindsight-style): rewrite tentative IDs to canonical
            # IDs BEFORE storage so different surface forms that match an
            # existing canonical never produce duplicate entity rows. The
            # post-store ``resolve()`` alias pass becomes redundant in this
            # mode and is skipped below.
            if self.entity_resolver.canonical_resolution:
                try:
                    await self.entity_resolver.resolve_canonical_ids_in_place(
                        new_entities=entities,
                        bank_id=request.bank_id,
                        graph_store=self.graph_store,
                        event_date=request.occurred_at,
                    )
                except Exception as exc:
                    _logger.warning(
                        "canonical resolution failed during retain (falling "
                        "back to tentative IDs): %s", exc,
                    )

        entity_ids = await self.graph_store.store_entities(entities, request.bank_id)
        associations = [
            MemoryEntityAssociation(memory_id=mid, entity_id=eid)
            for mid in memory_ids
            for eid in entity_ids
        ]
        await self.graph_store.link_memories_to_entities(associations, request.bank_id)
        if len(entity_ids) > 1:
            links = [
                EntityLink(
                    entity_a=entity_ids[i],
                    entity_b=entity_ids[j],
                    link_type="co_occurs",
                )
                for i in range(len(entity_ids))
                for j in range(i + 1, len(entity_ids))
            ]
            await self.graph_store.store_links(links, request.bank_id)

        # Legacy two-stage flow: post-store alias resolution. Skipped when
        # ``canonical_resolution=True`` because IDs are already canonical.
        if (
            self.entity_resolver is not None
            and not self.entity_resolver.canonical_resolution
        ):
            try:
                await self.entity_resolver.resolve(
                    new_entities=entities,
                    source_text=prepared.text,
                    bank_id=request.bank_id,
                    graph_store=self.graph_store,
                    llm_provider=self.llm_provider,
                    event_date=request.occurred_at,
                )
            except Exception as exc:
                _logger.warning("entity resolution failed during retain: %s", exc)

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

        # 3. Extract entities (profile can disable; metadata profiles avoid LLM calls)
        if profile is not None and profile.entity_extraction == "metadata":
            entities = _entities_from_metadata(prepared.metadata)
        elif prepared.extract_entities:
            entities = await extract_entities(prepared.text, self.llm_provider)
        else:
            entities = []

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
                    retained_at=datetime.now(timezone.utc),  # M9: wall-clock store time
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
            # 5a. Attach name embeddings before persistence so the
            # Hindsight-inspired entity-resolution cascade has the cheap
            # cosine-similarity tier available.  Only fires when an
            # entity_resolver is wired up — without one, the embedding
            # would be persisted but never used, wasting API budget.
            if self.entity_resolver is not None:
                await self._attach_entity_name_embeddings(entities)
                # Path B (Hindsight): rewrite tentative IDs to canonicals
                # before storage. Skipped when canonical_resolution=False
                # to preserve the legacy two-stage (store → resolve aliases)
                # flow.
                if self.entity_resolver.canonical_resolution:
                    try:
                        await self.entity_resolver.resolve_canonical_ids_in_place(
                            new_entities=entities,
                            bank_id=request.bank_id,
                            graph_store=self.graph_store,
                            event_date=request.occurred_at,
                        )
                    except Exception as exc:
                        _logger.warning(
                            "canonical resolution failed during retain "
                            "(falling back to tentative IDs): %s", exc,
                        )

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
                        entity_a=entity_ids[i],
                        entity_b=entity_ids[j],
                        link_type="co_occurs",
                    )
                    for i in range(len(entity_ids))
                    for j in range(i + 1, len(entity_ids))
                ]
                await self.graph_store.store_links(links, request.bank_id)

            # 5b. Entity resolution (M11) — Hindsight-inspired tiered cascade.
            # Skipped when canonical_resolution=True (Path B) because IDs
            # are already canonical from the pre-store pass above. Otherwise
            # the legacy two-stage path runs the cascade and writes
            # alias_of links for matched candidates.
            if (
                self.entity_resolver is not None
                and not self.entity_resolver.canonical_resolution
            ):
                try:
                    await self.entity_resolver.resolve(
                        new_entities=entities,
                        source_text=prepared.text,
                        bank_id=request.bank_id,
                        graph_store=self.graph_store,
                        llm_provider=self.llm_provider,
                        event_date=request.occurred_at,
                    )
                except Exception as exc:
                    # Resolution failures must never abort a retain — degrade gracefully.
                    _logger.warning("entity resolution failed during retain: %s", exc)

        # 6. Update dedup cache with stored embeddings
        for mem_id, emb in zip(memory_ids, embeddings):
            self._dedup.add(request.bank_id, mem_id, emb)

        # 7. Observation consolidation (fire-and-forget async task).
        # Runs after the retain response is returned so it never adds latency
        # to the caller.  A failure here must never surface to the caller —
        # the raw memories are already stored and the consolidation is
        # best-effort.  The representative vector is the first chunk's
        # embedding, which is sufficient for the observation similarity search.
        if self._observation_consolidator is not None and memory_ids and chunks:
            representative_vec = embeddings[0]
            consolidator = self._observation_consolidator
            bank_id = request.bank_id
            first_chunk = chunks[0]
            all_ids = list(memory_ids)
            vs = self.vector_store
            llm = self.llm_provider

            async def _run_consolidation() -> None:
                try:
                    await consolidator.consolidate(
                        new_memory_text=first_chunk,
                        new_memory_ids=all_ids,
                        bank_id=bank_id,
                        vector_store=vs,
                        llm_provider=llm,
                        query_vector=representative_vec,
                    )
                except Exception as exc:
                    _logger.warning(
                        "Observation consolidation task failed for bank %s: %s", bank_id, exc
                    )

            task = asyncio.create_task(_run_consolidation())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return RetainResult(
            stored=True,
            memory_id=memory_ids[0] if memory_ids else None,
        )

    async def retain_many(self, requests: list[RetainRequest]) -> list[RetainResult]:
        """Retain multiple requests while batching embedding generation and vector writes."""

        if not requests:
            return []

        results: list[RetainResult | None] = [None] * len(requests)
        records: list[dict[str, Any]] = []
        all_chunks: list[str] = []
        profile_table = merged_user_and_builtin_profiles(self.extraction_profiles)

        for index, request in enumerate(requests):
            profile = profile_table.get(request.extraction_profile) if request.extraction_profile else None
            mip_pipeline = request.mip_pipeline
            mip_chunker = mip_pipeline.chunker if mip_pipeline else None
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
                results[index] = RetainResult(stored=False, error="No content after chunking")
                continue

            start = len(all_chunks)
            all_chunks.extend(chunks)
            records.append({
                "index": index,
                "request": request,
                "profile": profile,
                "prepared": prepared,
                "chunks": chunks,
                "start": start,
                "end": len(all_chunks),
            })

        if not records:
            return [result or RetainResult(stored=False, error="No content after chunking") for result in results]

        all_embeddings = await generate_embeddings(all_chunks, self.llm_provider)
        stored_records: list[dict[str, Any]] = []
        all_items: list[VectorItem] = []

        for record in records:
            request: RetainRequest = record["request"]
            profile: ExtractionProfileConfig | None = record["profile"]
            prepared = record["prepared"]
            chunks = record["chunks"]
            embeddings = all_embeddings[record["start"]:record["end"]]
            mip_pipeline = request.mip_pipeline
            mip_dedup = mip_pipeline.dedup if mip_pipeline else None
            dedup_threshold_override = mip_dedup.threshold if mip_dedup else None
            dedup_action = (mip_dedup.action if mip_dedup else None) or "skip_chunk"

            keep_indices: list[int] = []
            any_duplicate = False
            for chunk_index, embedding in enumerate(embeddings):
                is_dup, _sim = self._dedup.is_duplicate(
                    request.bank_id,
                    embedding,
                    threshold_override=dedup_threshold_override,
                )
                if is_dup:
                    any_duplicate = True
                if dedup_action == "warn" or not is_dup:
                    keep_indices.append(chunk_index)

            result_index = record["index"]
            if dedup_action == "skip" and any_duplicate:
                results[result_index] = RetainResult(
                    stored=False,
                    deduplicated=True,
                    error="Duplicate chunk(s) found; rule action=skip",
                )
                continue
            if not keep_indices:
                results[result_index] = RetainResult(
                    stored=False,
                    deduplicated=True,
                    error="All chunks are near-duplicates",
                )
                continue

            chunks = [chunks[i] for i in keep_indices]
            embeddings = [embeddings[i] for i in keep_indices]
            if profile is not None and profile.entity_extraction == "metadata":
                entities = _entities_from_metadata(prepared.metadata)
            elif prepared.extract_entities:
                entities = await extract_entities(prepared.text, self.llm_provider)
            else:
                entities = []

            profile_tier = profile.authority_tier if profile else None
            chunk_metadata = merge_retain_metadata_authority_tier(
                prepared.metadata,
                bank_id=request.bank_id,
                profile_authority_tier=profile_tier,
                recall_authority=self.recall_authority,
            )
            chunk_metadata = dict(chunk_metadata or {})
            if request.mip_rule_name:
                chunk_metadata["_mip.rule"] = request.mip_rule_name
            if mip_pipeline and mip_pipeline.version is not None:
                chunk_metadata["_mip.pipeline_version"] = int(mip_pipeline.version)
            chunk_metadata.setdefault("_created_at", datetime.now(timezone.utc).isoformat())

            memory_ids: list[str] = []
            items: list[VectorItem] = []
            for chunk, embedding in zip(chunks, embeddings, strict=False):
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
                        retained_at=datetime.now(timezone.utc),
                    )
                )

            record.update({
                "chunks": chunks,
                "embeddings": embeddings,
                "entities": entities,
                "memory_ids": memory_ids,
                "items": items,
            })
            stored_records.append(record)
            all_items.extend(items)

        if all_items:
            await self.vector_store.store_vectors(all_items)

        if self.document_store is not None:
            from astrocyte.types import Document

            for record in stored_records:
                request = record["request"]
                for item in record["items"]:
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
                            item.id,
                            exc,
                        )

        # ── Phase 1: parallel entity processing across all records ──
        # Each record's entity work (embed names, store entities, write
        # co-occurrence links, run resolution cascade) is independent of
        # other records and dominates retain wall-clock when the bank is
        # large. ``asyncio.gather`` lets all records' LLM/DB I/O run
        # concurrently — for batches of 10 records this gives ~10× speedup
        # on the entity-resolution-bound retain phase. The non-I/O steps
        # (dedup cache, result assembly) stay sequential below.
        if self.graph_store is not None:
            entity_records = [r for r in stored_records if r["entities"]]
            if entity_records:
                await asyncio.gather(*[
                    self._process_record_entities(r) for r in entity_records
                ])

        for record in stored_records:
            request: RetainRequest = record["request"]
            prepared = record["prepared"]
            memory_ids: list[str] = record["memory_ids"]
            embeddings: list[list[float]] = record["embeddings"]
            chunks: list[str] = record["chunks"]
            entities = record["entities"]

            for mem_id, embedding in zip(memory_ids, embeddings, strict=False):
                self._dedup.add(request.bank_id, mem_id, embedding)

            if self._observation_consolidator is not None and memory_ids and chunks:
                representative_vec = embeddings[0]
                consolidator = self._observation_consolidator
                bank_id = request.bank_id
                first_chunk = chunks[0]
                all_ids = list(memory_ids)
                vs = self.vector_store
                llm = self.llm_provider

                async def _run_consolidation() -> None:
                    try:
                        await consolidator.consolidate(
                            new_memory_text=first_chunk,
                            new_memory_ids=all_ids,
                            bank_id=bank_id,
                            vector_store=vs,
                            llm_provider=llm,
                            query_vector=representative_vec,
                        )
                    except Exception as exc:
                        _logger.warning(
                            "Observation consolidation task failed for bank %s: %s",
                            bank_id,
                            exc,
                        )

                task = asyncio.create_task(_run_consolidation())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

            results[record["index"]] = RetainResult(
                stored=True,
                memory_id=memory_ids[0] if memory_ids else None,
            )

        return [result or RetainResult(stored=False, error="Not retained") for result in results]

    async def recall(self, request: RecallRequest) -> RecallResult:
        """Recall pipeline: embed query → parallel retrieve → fuse → rerank → budget."""
        # 1. Embed query
        query_embeddings = await generate_embeddings([request.query], self.llm_provider)
        query_vector = query_embeddings[0]

        # 1b. Wiki-tier precedence (M8 W5) — search compiled wiki pages first.
        # If the top hit scores above wiki_confidence_threshold, return wiki hits
        # + raw-memory citations and skip the full parallel-retrieve pipeline.
        # Caller can bypass this tier by setting fact_types (which implies they
        # want raw memories, not compiled wiki pages).
        if self.wiki_store is not None and not request.fact_types:
            wiki_result = await self._try_wiki_tier(request, query_vector)
            if wiki_result is not None:
                return wiki_result

        # 2. Build filters
        filters = VectorFilters(
            bank_id=request.bank_id,
            tags=request.tags,
            fact_types=request.fact_types,
            time_range=request.time_range,
            as_of=request.as_of,  # M9: time-travel filter
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

        # 2c. HyDE (R1) — generate hypothetical document embedding.
        # Runs concurrently with the retrieval step below via a separate task.
        # Failures return None; parallel_retrieve treats None as "HyDE disabled".
        hyde_vec: list[float] | None = None
        if self.enable_hyde:
            hyde_vec = await generate_hyde_vector(request.query, self.llm_provider)

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
            hyde_vector=hyde_vec,
        )
        query_plan = build_query_plan(request.query)
        if (
            not request.fact_types
            and query_plan.needs_multi_hop_synthesis
        ):
            entity_path_hits = await self._retrieve_entity_path_fallback(
                request.query,
                request.bank_id,
                limit=overfetch_limit,
            )
            if entity_path_hits:
                strategy_results["entity_path"] = entity_path_hits

        # 3b. Intent classification — hoisted here so both observation injection
        # (step 3b) and RRF weighting (step 4) share the same result without
        # computing it twice. UNKNOWN → neutral 1.0 weights everywhere.
        query_intent = QueryIntent.UNKNOWN
        if self.enable_intent_aware_recall:
            query_intent = classify_query_intent(request.query).intent
        intent_weights = weights_for_intent(query_intent)

        # 3c. Observation strategy — intent-gated injection.
        # The ::obs bank holds distilled, multi-evidence facts synthesised from
        # raw memories.  Injecting it for *every* recall degrades factual
        # precision (abstract summaries displace verbatim answers).  Instead,
        # inject only for EXPLORATORY and RELATIONAL queries — the two intents
        # where synthesised behavioural patterns add value over raw memories:
        #   EXPLORATORY: "What are Alice's hobbies?" / "Describe Bob's personality"
        #   RELATIONAL:  "How does Alice's role relate to her projects?"
        # For all other intents, effective_obs_weight falls back to the
        # configured observation_weight (default 0.0 = disabled).
        from astrocyte.pipeline.reflect import _auto_prompt_variant

        _OBS_INJECTION_INTENTS = {QueryIntent.EXPLORATORY, QueryIntent.RELATIONAL}
        prompt_variant = _auto_prompt_variant(request.query)
        effective_obs_weight = (
            self.observation_injection_weight
            if query_intent in _OBS_INJECTION_INTENTS or prompt_variant == "evidence_inference"
            else self.observation_weight
        )
        if (
            self._observation_consolidator is not None
            and not request.fact_types
            and effective_obs_weight > 0.0
        ):
            obs_results = await self._retrieve_observations(
                query_vector, request.bank_id, overfetch_limit, request.as_of
            )
            if obs_results:
                strategy_results["observation"] = obs_results

        # 4. RRF fusion (local strategies + optional federated / manual external_context)
        weighted_inputs: list[tuple[list[Any], float]] = []
        for strategy, results in strategy_results.items():
            if not results:
                continue
            # Observation strategy uses the effective intent-gated weight;
            # other strategies use intent-derived weights.
            if strategy == "observation":
                weight = effective_obs_weight
            else:
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

        # 4b. Multi-query expansion: decompose the question into sub-questions,
        # recall for each independently (parallel), and merge all fused lists
        # via a final RRF pass. Only runs when enabled and the LLM judges the
        # question as multi-hop (len > 1). The original query's fused result is
        # always included so the expansion never discards the baseline recall.
        #
        # Two guards:
        # (a) Empty-bank guard — if initial retrieval returned nothing,
        #     decomposition cannot help and we avoid a wasted LLM call.
        # (b) Confidence gate — peek at the top raw semantic score *before*
        #     fusion.  Cosine similarity is the cleanest signal for "did we
        #     already find the answer?": if the top hit already exceeds
        #     multi_query_confidence_threshold we skip decomposition entirely,
        #     preventing broad sub-queries from displacing the precise answer.
        #     RRF scores are rank-based and not used here.
        _top_semantic_score: float = 0.0
        if "semantic" in strategy_results and strategy_results["semantic"]:
            _top_semantic_score = strategy_results["semantic"][0].score

        if (
            self.enable_multi_query_expansion
            and fused
            and _top_semantic_score < self.multi_query_confidence_threshold
        ):
            from astrocyte.pipeline.multi_query import decompose_query

            sub_queries = await decompose_query(request.query, self.llm_provider)
            if len(sub_queries) > 1:
                async def _fuse_sub_query(sq: str) -> list[Any]:
                    sq_vec = (await generate_embeddings([sq], self.llm_provider))[0]
                    sq_strategy_results = await parallel_retrieve(
                        query_vector=sq_vec,
                        query_text=sq,
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
                    sq_intent = (
                        classify_query_intent(sq).intent
                        if self.enable_intent_aware_recall
                        else QueryIntent.UNKNOWN
                    )
                    sq_weights = weights_for_intent(sq_intent)
                    sq_weighted = [
                        (res, getattr(sq_weights, strat, 1.0))
                        for strat, res in sq_strategy_results.items()
                        if res
                    ]
                    if not sq_weighted:
                        return []
                    if all(w == 1.0 for _, w in sq_weighted):
                        return rrf_fusion([r for r, _ in sq_weighted], k=self.rrf_k)
                    return weighted_rrf_fusion(sq_weighted, k=self.rrf_k)

                # sub_queries[0] is the original (already in fused); expand the rest
                sub_fused_lists = await asyncio.gather(
                    *[_fuse_sub_query(sq) for sq in sub_queries[1:]]
                )
                non_empty = [sf for sf in sub_fused_lists if sf]
                if non_empty:
                    fused = rrf_fusion([fused, *non_empty], k=self.rrf_k)

        # 5. Reranking — apply per-bank MIP RerankSpec when a rule targets this bank (P3)
        # (bank_pipeline already resolved above for temporal half-life override)
        mip_rerank = bank_pipeline.rerank if bank_pipeline is not None else None
        reranked = basic_rerank(fused, request.query, mip_rerank=mip_rerank)
        if self.final_rerank_mode == "llm_pairwise":
            reranked = await llm_pairwise_rerank(
                reranked,
                request.query,
                self.llm_provider,
                top_n=self.final_rerank_top_n,
                keep_n=self.final_rerank_keep_n or len(reranked),
            )
        else:
            reranked = apply_context_diversity(reranked, request.query)

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
                occurred_at=getattr(item, "occurred_at", None),
                retained_at=getattr(item, "retained_at", None),  # M9
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

    async def _retrieve_observations(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int,
        as_of: Any | None,
    ) -> list[Any]:
        """Retrieve observation-layer hits for RRF fusion.

        Observations are stored in a dedicated bank (``{bank_id}::obs``) that
        is completely separate from the raw memory bank.  This prevents
        double-counting: the main semantic/keyword/temporal strategies only
        search the raw bank, while this method exclusively searches the obs
        bank.  Converts ``VectorHit`` objects to ``ScoredItem`` to match the
        format expected by the weighted RRF fusion step.
        """
        from astrocyte.pipeline.fusion import ScoredItem
        from astrocyte.pipeline.observation import obs_bank_id

        obs_bank = obs_bank_id(bank_id)
        obs_filters = VectorFilters(
            bank_id=obs_bank,
            as_of=as_of,
        )
        try:
            hits = await self.vector_store.search_similar(
                query_vector, obs_bank, limit=limit, filters=obs_filters
            )
        except Exception as exc:
            _logger.warning("Observation retrieval failed for bank %s: %s", bank_id, exc)
            return []

        return [
            ScoredItem(
                id=h.id,
                text=h.text,
                score=h.score,
                fact_type=h.fact_type,
                metadata=h.metadata,
                tags=h.tags,
                memory_layer="observation",
                retained_at=getattr(h, "retained_at", None),
            )
            for h in hits
        ]

    async def _retrieve_entity_path_fallback(
        self,
        query: str,
        bank_id: str,
        *,
        limit: int,
    ) -> list[ScoredItem]:
        """In-memory entity-path recall when no graph backend is configured."""

        query_names = _deterministic_names(query)
        if not query_names:
            return []
        try:
            items = await self.vector_store.list_vectors(bank_id, offset=0, limit=max(limit * 10, 200))
        except Exception:
            return []

        hits: list[ScoredItem] = []
        for item in items:
            metadata = dict(item.metadata or {})
            text_names = _deterministic_names(item.text)
            metadata_names = {
                part.strip().lower()
                for key in ("locomo_persons", "locomo_speakers", "person")
                for part in str(metadata.get(key) or "").replace("|", ",").split(",")
                if part.strip()
            }
            matched = query_names & (text_names | metadata_names)
            if not matched:
                continue
            metadata["_entity_path"] = " -> ".join(sorted(matched))
            metadata["_entity_path_kind"] = "metadata_fallback"
            hits.append(
                ScoredItem(
                    id=item.id,
                    text=item.text,
                    score=0.65 + min(len(matched), 3) * 0.05,
                    fact_type=item.fact_type,
                    metadata=metadata,
                    tags=item.tags,
                    memory_layer=item.memory_layer,
                    occurred_at=item.occurred_at,
                    retained_at=item.retained_at,
                )
            )
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]

    async def _try_wiki_tier(
        self,
        request: RecallRequest,
        query_vector: list[float],
    ) -> RecallResult | None:
        """Search compiled wiki pages and return hits if the top score meets the threshold.

        Returns ``None`` when:
        - No wiki pages exist in the bank.
        - The top wiki score is below ``wiki_confidence_threshold``.
        - The vector store raises (wiki tier is non-fatal; full recall continues).

        When a wiki hit is returned the result also includes raw-memory citations
        derived from ``_wiki_source_ids`` stored in VectorItem metadata during
        compile.  The citations are returned as additional MemoryHits with
        ``memory_layer="raw"`` so callers can distinguish synthesised wiki
        content from the underlying evidence.
        """
        try:
            wiki_filters = VectorFilters(
                bank_id=request.bank_id,
                tags=request.tags,
                fact_types=["wiki"],
            )
            wiki_hits = await self.vector_store.search_similar(
                query_vector,
                request.bank_id,
                limit=request.max_results,
                filters=wiki_filters,
            )
        except Exception:
            _logger.debug("Wiki-tier search failed; falling back to standard recall", exc_info=True)
            return None

        if not wiki_hits or wiki_hits[0].score < self.wiki_confidence_threshold:
            return None

        # Convert wiki VectorHits → MemoryHits
        hits: list[MemoryHit] = [
            MemoryHit(
                text=h.text,
                score=h.score,
                fact_type=h.fact_type,
                metadata=h.metadata,
                tags=h.tags,
                memory_id=h.id,
                bank_id=request.bank_id,
                memory_layer="compiled",
            )
            for h in wiki_hits
        ]

        # Append raw-memory citations from source_ids stored in metadata
        citation_ids: list[str] = []
        for h in wiki_hits:
            raw_ids_str = (h.metadata or {}).get("_wiki_source_ids", "")
            if raw_ids_str:
                citation_ids.extend(raw_ids_str.split(","))

        if citation_ids:
            # Fetch raw memories by scanning the vector store.
            # We use list_vectors (paginated) to find matches by ID — the
            # VectorStore SPI has no get_by_ids, so we scan once and filter.
            raw_map: dict[str, VectorItem] = {}
            offset = 0
            batch = 100
            target = set(citation_ids)
            while target:
                chunk = await self.vector_store.list_vectors(request.bank_id, offset=offset, limit=batch)
                if not chunk:
                    break
                for item in chunk:
                    if item.id in target:
                        raw_map[item.id] = item
                        target.discard(item.id)
                if len(chunk) < batch:
                    break
                offset += batch

            for cid in citation_ids:
                item = raw_map.get(cid)
                if item is None:
                    continue
                hits.append(
                    MemoryHit(
                        text=item.text,
                        score=0.0,  # citations are provenance, not ranked hits
                        fact_type=item.fact_type,
                        metadata=item.metadata,
                        tags=item.tags,
                        memory_id=item.id,
                        bank_id=request.bank_id,
                        memory_layer="raw",
                    )
                )

        # Apply token budget if requested
        truncated = False
        if request.max_tokens:
            hits, truncated = enforce_token_budget(hits, request.max_tokens)

        return RecallResult(
            hits=hits[: request.max_results],
            total_available=len(wiki_hits),
            truncated=truncated,
            trace=RecallTrace(
                strategies_used=["wiki"],
                total_candidates=len(wiki_hits),
                fusion_method="wiki_tier",
                wiki_tier_used=True,
            ),
        )

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        """Reflect pipeline: recall → LLM synthesis."""
        query_plan = build_query_plan(request.query)

        # 1. Run recall with larger result set. Aggregate/multi-hop queries need
        # more candidate memories so synthesis can combine facts instead of
        # answering from the first plausible hit.
        recall_request = RecallRequest(
            query=request.query,
            bank_id=request.bank_id,
            max_results=query_plan.recall_max_results,
            max_tokens=request.max_tokens,
        )
        recall_result = await self.recall(recall_request)
        expanded_hits = await self._expand_reflect_sources(
            request.bank_id,
            self._rank_reflect_context(
                request.query,
                recall_result.hits,
                limit=query_plan.reflect_rank_limit,
            ),
            limit=query_plan.reflect_expand_limit,
        )
        recall_result.hits = expanded_hits
        if request.max_tokens:
            recall_result.hits, expanded_truncated = enforce_token_budget(
                recall_result.hits,
                request.max_tokens,
            )
            recall_result.truncated = recall_result.truncated or expanded_truncated

        path_ctx = self._entity_path_authority_context(recall_result.hits)
        auth_ctx: str | None = path_ctx
        ra = self.recall_authority
        if ra and ra.enabled and ra.apply_to_reflect:
            recall_result = apply_recall_authority(recall_result, ra)
            auth_ctx = "\n\n".join(part for part in (path_ctx, recall_result.authority_context) if part)

        # 2. Resolve per-bank ReflectSpec from MIP (Phase 2, Step 9)
        mip_reflect = None
        if self.mip_router is not None:
            bank_pipeline = self.mip_router.resolve_pipeline_for_bank(request.bank_id)
            if bank_pipeline is not None:
                mip_reflect = bank_pipeline.reflect

        # 2b. Auto-select a prompt when no MIP prompt override is set.  The
        # classifier is a zero-cost regex heuristic; explicit MIP prompts
        # always win over this automatic routing.
        if mip_reflect is None or mip_reflect.prompt is None:
            from astrocyte.mip.schema import ReflectSpec
            from astrocyte.pipeline.reflect import _auto_prompt_variant

            prompt_variant = query_plan.prompt_variant or _auto_prompt_variant(request.query)
            if prompt_variant is not None:
                if mip_reflect is None:
                    mip_reflect = ReflectSpec(prompt=prompt_variant)
                else:
                    # Preserve any other MIP settings (e.g. promote_metadata)
                    mip_reflect = ReflectSpec(
                        prompt=prompt_variant,
                        promote_metadata=mip_reflect.promote_metadata,
                    )

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

    async def shutdown(self) -> None:
        """Drain background work and close provider resources owned by the pipeline."""
        if self._background_tasks:
            _, pending = await asyncio.wait(self._background_tasks, timeout=2.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        for provider in (self.vector_store, self.graph_store, self.document_store):
            close = getattr(provider, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    _ = await result
            except Exception as exc:
                _logger.warning("provider close failed during pipeline shutdown: %s", exc)

    def _rank_reflect_context(
        self,
        query: str,
        hits: list[MemoryHit],
        *,
        limit: int,
    ) -> list[MemoryHit]:
        """Apply final precision rerank and hierarchy before synthesis."""
        if not hits:
            return hits

        scored = cross_encoder_like_rerank(
            [
                ScoredItem(
                    id=h.memory_id or f"hit-{idx}",
                    text=h.text,
                    score=h.score,
                    fact_type=h.fact_type,
                    metadata=h.metadata,
                    tags=h.tags,
                    memory_layer=h.memory_layer,
                    occurred_at=h.occurred_at,
                    retained_at=h.retained_at,
                )
                for idx, h in enumerate(hits)
            ],
            query,
        )
        hit_by_id = {h.memory_id or f"hit-{idx}": h for idx, h in enumerate(hits)}
        return [hit_by_id[item.id] for item in scored[:limit] if item.id in hit_by_id]

    def _entity_path_authority_context(self, hits: list[MemoryHit]) -> str | None:
        path_lines: list[str] = []
        direct_lines: list[str] = []
        for idx, hit in enumerate(hits, 1):
            path = (hit.metadata or {}).get("_entity_path") if hit.metadata else None
            if path:
                path_lines.append(f"- Memory {idx}: entity_path={path}")
            elif hit.score >= 0.5:
                direct_lines.append(f"- Memory {idx}: direct_facts")
        if not path_lines:
            return None
        sections = ["entity_path_evidence:", *path_lines]
        if direct_lines:
            sections.extend(["direct_facts:", *direct_lines[:8]])
        sections.append("supporting_context: use entity-path evidence before unrelated semantic matches.")
        return "\n".join(sections)

    async def _expand_reflect_sources(
        self,
        bank_id: str,
        hits: list[MemoryHit],
        *,
        limit: int,
    ) -> list[MemoryHit]:
        """Append raw sources cited by top wiki/observation hits.

        This mirrors Hindsight's reflect loop in a bounded, non-agentic form:
        start from compiled/observation evidence, then expand to raw facts for
        grounding before synthesis.
        """
        if not hits:
            return hits

        source_ids: list[str] = []
        seen_sources: set[str] = set()
        for hit in hits:
            for sid in _source_ids_from_metadata(hit.metadata):
                if sid not in seen_sources:
                    seen_sources.add(sid)
                    source_ids.append(sid)
        if not source_ids:
            return hits[:limit]

        raw_hits = await self._fetch_memory_hits_by_id(bank_id, source_ids)
        existing_ids = {h.memory_id for h in hits if h.memory_id}
        expanded = list(hits)
        for raw in raw_hits:
            if raw.memory_id and raw.memory_id not in existing_ids:
                expanded.append(raw)
                existing_ids.add(raw.memory_id)
            if len(expanded) >= limit:
                break
        return expanded[:limit]

    async def _fetch_memory_hits_by_id(self, bank_id: str, ids: list[str]) -> list[MemoryHit]:
        target = set(ids)
        found: dict[str, VectorItem] = {}
        offset = 0
        batch = 100
        while target:
            chunk = await self.vector_store.list_vectors(bank_id, offset=offset, limit=batch)
            if not chunk:
                break
            for item in chunk:
                if item.id in target:
                    found[item.id] = item
                    target.discard(item.id)
            if len(chunk) < batch:
                break
            offset += batch

        return [
            MemoryHit(
                text=item.text,
                score=0.0,
                fact_type=item.fact_type,
                metadata=item.metadata,
                tags=item.tags,
                memory_id=item.id,
                bank_id=bank_id,
                memory_layer=item.memory_layer or "raw",
                occurred_at=item.occurred_at,
                retained_at=item.retained_at,
            )
            for sid in ids
            if (item := found.get(sid)) is not None
        ]


def _source_ids_from_metadata(metadata: dict[str, Any] | None) -> list[str]:
    if not metadata:
        return []
    raw = metadata.get("_obs_source_ids") or metadata.get("_wiki_source_ids")
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x]
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    return []


def _deterministic_names(text: str) -> set[str]:
    return {
        match.group(0).strip().lower()
        for match in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b", text or "")
    }


def _entities_from_metadata(metadata: dict[str, Any] | None) -> list[Entity]:
    """Build stable entities from structured retain metadata without an LLM call."""

    if not metadata:
        return []
    names: set[str] = set()
    for key in ("locomo_persons", "locomo_speakers", "person"):
        value = metadata.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            names.update(part.strip() for part in value.replace("|", ",").split(",") if part.strip())
        elif isinstance(value, list):
            names.update(str(part).strip() for part in value if str(part).strip())

    entities: list[Entity] = []
    for name in sorted(names):
        entity_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if not entity_id:
            continue
        entities.append(
            Entity(
                id=f"person:{entity_id}",
                name=name,
                entity_type="PERSON",
                aliases=[],
                metadata={"source": "retain_metadata"},
            )
        )
    return entities
