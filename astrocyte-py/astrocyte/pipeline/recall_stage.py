from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from astrocyte.pipeline.embedding import generate_embeddings
from astrocyte.pipeline.entity_extraction import extract_entities
from astrocyte.pipeline.fusion import (
    ScoredItem,
    memory_hits_as_scored,
    rrf_fusion,
    weighted_rrf_fusion,
)
from astrocyte.pipeline.hyde import generate_hyde_vector
from astrocyte.pipeline.link_expansion import link_expansion
from astrocyte.pipeline.query_intent import (
    QueryIntent,
    classify_query_intent,
    weights_for_intent,
)
from astrocyte.pipeline.query_plan import build_query_plan
from astrocyte.pipeline.reranking import (
    apply_context_diversity,
    basic_rerank,
    llm_pairwise_rerank,
)
from astrocyte.pipeline.retrieval import parallel_retrieve
from astrocyte.policy.homeostasis import enforce_token_budget
from astrocyte.types import (
    MemoryHit,
    RecallRequest,
    RecallResult,
    RecallTrace,
    VectorFilters,
    VectorItem,
)

if TYPE_CHECKING:
    pass



from astrocyte.pipeline._orchestrator_common import (
    _deterministic_names,
    _warn_on_version_drift,
)

_logger = logging.getLogger("astrocyte.mip")


class RecallStageMixin:
    """Recall pipeline: parallel retrieve → fuse → expand → rerank.

    A behavior slice of PipelineOrchestrator, composed into it as a mixin.
    ``self`` is the full orchestrator at runtime (attributes come from its
    ``__init__`` / ``apply_config``); shared helpers live on sibling mixins.
    """

    async def _expand_via_sibling_chunks(
        self,
        fused: list,
        bank_id: str,
    ) -> list:
        """M10: extend ``fused`` with vectors from sibling chunks of each top-K hit.

        Walks the top-K hits, asks the SourceStore for the sibling chunks
        of each hit's ``chunk_id`` (capped at ``expansion_max_per_hit``),
        then asks the vector store for the vectors backing those chunks.
        Each expanded hit's score is multiplied by
        ``expansion_score_multiplier`` so the seed hits stay ranked
        higher (the multiplier is < 1.0 by default).

        Returns the merged list, sorted by score descending. Existing
        memory ids are NOT duplicated — siblings whose vector is already
        in ``fused`` are skipped.
        """
        if not fused:
            return fused
        store = self.source_store
        if store is None:
            return fused

        # 1. Collect (chunk_id, doc_id?) for the top-K seeds. We process
        # only the top ``source_expansion_max_per_hit * 4`` because the
        # multiplier already shrinks tail-hit gains and we want to keep
        # the per-recall fan-out bounded.
        seed_cap = max(1, self.source_expansion_max_per_hit * 4)
        seed_chunk_ids = [getattr(h, "chunk_id", None) for h in fused[:seed_cap] if getattr(h, "chunk_id", None)]
        if not seed_chunk_ids:
            return fused

        # 2. Resolve each seed chunk → its document, then list siblings
        # of that document. Run lookups in parallel; tolerate failures.
        async def _siblings_for(chunk_id: str) -> list[str]:
            try:
                chunk = await store.get_chunk(chunk_id, bank_id)  # type: ignore[union-attr]
                if chunk is None:
                    return []
                siblings = await store.list_chunks(chunk.document_id, bank_id)  # type: ignore[union-attr]
                # Drop the seed chunk itself; cap at expansion_max_per_hit.
                ids = [c.id for c in siblings if c.id != chunk_id][: self.source_expansion_max_per_hit]
                return ids
            except Exception as exc:
                _logger.debug("source_store sibling lookup failed for %s: %s", chunk_id, exc)
                return []

        sibling_lists = await asyncio.gather(*(_siblings_for(cid) for cid in seed_chunk_ids))
        sibling_chunk_ids: set[str] = set()
        for ids in sibling_lists:
            sibling_chunk_ids.update(ids)
        # Drop any chunk_ids already represented in ``fused`` so we don't
        # re-rank the same memory twice.
        already_present = {getattr(h, "chunk_id", None) for h in fused if getattr(h, "chunk_id", None)}
        sibling_chunk_ids -= already_present
        if not sibling_chunk_ids:
            return fused

        # 3. Pull the sibling vectors. The vector store returns score=1.0;
        # we apply the configured multiplier so seeds stay ranked higher.
        try:
            sibling_hits = await self.vector_store.get_by_chunk_ids(  # type: ignore[attr-defined]
                list(sibling_chunk_ids),
                bank_id,
            )
        except Exception as exc:
            _logger.warning("vector_store.get_by_chunk_ids failed; skipping chunk expansion: %s", exc)
            return fused

        if not sibling_hits:
            return fused

        existing_ids = {getattr(h, "id", None) for h in fused}
        boost = self.source_expansion_score_multiplier
        for sh in sibling_hits:
            if sh.id in existing_ids:
                continue
            sh.score = max(0.0, min(1.0, sh.score * boost))
            fused.append(sh)

        # Re-sort: seeds keep their fused score, expansions slot in by
        # the boosted score. The downstream rerank stage gets the final say.
        fused.sort(key=lambda h: getattr(h, "score", 0.0), reverse=True)
        return fused

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
            session_id=request.session_id,  # M31 Fix 2
        )

        # 2a. Query-level temporal constraint extraction. The analyzer
        # parses expressions like "what happened in March 2024?" or
        # "last week's events" into a (start, end) range that becomes
        # an extra time_range filter on top of any caller-supplied one.
        # When the request already supplies a time_range, the
        # analyzer's hit is the more restrictive of the two — the
        # caller's filter remains the floor.
        if (
            self.query_analyzer_enabled and request.time_range is None  # caller-supplied range wins
        ):
            try:
                from astrocyte.pipeline.query_analyzer import analyze_query

                # Resolution order for the relative-phrase anchor:
                #   1. ``query_reference_date`` if explicitly set
                #      (preferred — see types.py for rationale)
                #   2. ``as_of`` if set (back-compat: M9 audit-replay
                #      callers got temporal anchoring "for free" before
                #      the split — preserve that)
                #   3. ``None`` → analyze_query falls back to ``now()``
                anchor = request.query_reference_date or request.as_of
                analysis = await analyze_query(
                    request.query,
                    reference_date=anchor,
                    llm_provider=self.llm_provider,
                    allow_llm_fallback=self.query_analyzer_allow_llm_fallback,
                    allow_temporal_expansion=self.query_analyzer_enable_temporal_expansion,
                )
                if analysis.temporal_constraint and analysis.temporal_constraint.is_bounded():
                    c = analysis.temporal_constraint
                    # VectorFilters.time_range is (start, end) tuple,
                    # both required for the SQL/in-memory adapters.
                    # Use bench-bank min/max as defaults for open
                    # ranges so the adapter contract is satisfied.
                    far_past = datetime(1900, 1, 1, tzinfo=timezone.utc)
                    far_future = datetime(2200, 1, 1, tzinfo=timezone.utc)
                    filters = VectorFilters(
                        bank_id=filters.bank_id,
                        tags=filters.tags,
                        fact_types=filters.fact_types,
                        time_range=(
                            c.start_date or far_past,
                            c.end_date or far_future,
                        ),
                        as_of=filters.as_of,
                        session_id=filters.session_id,  # M31 Fix 2 — preserve
                    )
                    _logger.info(
                        "query_analyzer extracted temporal range %s",
                        analysis.temporal_constraint,
                    )
            except Exception as exc:  # pragma: no cover — defensive
                _logger.warning(
                    "query_analyzer failed (%s); continuing without temporal filter.",
                    exc,
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
        strategy_timings_ms: dict[str, float] = {}
        strategy_candidate_counts: dict[str, int] = {}
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
            strategy_timings_ms=strategy_timings_ms,
            strategy_candidate_counts=strategy_candidate_counts,
            use_bm25_idf=self.bm25_idf_enabled,
        )
        query_plan = build_query_plan(request.query)
        if not request.fact_types and query_plan.needs_multi_hop_synthesis:
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
        if self._observation_consolidator is not None and not request.fact_types and effective_obs_weight > 0.0:
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

        # 4a. Link expansion (Hindsight parity, C3). After initial RRF
        # surfaces direct hits, query the three first-class memory-link
        # signals — entity overlap, semantic kNN, causal — and merge
        # the resulting candidates back into the fused set. Replaces
        # the previous BFS-hop spreading-activation path.
        if self.link_expansion_params is not None and self.graph_store is not None and fused:
            try:
                expansion_hits = await link_expansion(
                    fused[: self.link_expansion_params.expansion_limit],
                    bank_id=request.bank_id,
                    vector_store=self.vector_store,
                    graph_store=self.graph_store,
                    params=self.link_expansion_params,
                    tags=request.tags,
                )
            except Exception as exc:  # pragma: no cover — defensive
                _logger.warning(
                    "link expansion failed (%s); continuing with direct fused hits only.",
                    exc,
                )
                expansion_hits = []
            if expansion_hits:
                # Re-fuse via RRF so direct evidence keeps its precedence
                # (top-1 direct beats top-1 expansion by construction).
                fused = rrf_fusion([fused, expansion_hits], k=self.rrf_k)

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

        if self.enable_multi_query_expansion and fused and _top_semantic_score < self.multi_query_confidence_threshold:
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
                        use_bm25_idf=self.bm25_idf_enabled,
                    )
                    sq_intent = (
                        classify_query_intent(sq).intent if self.enable_intent_aware_recall else QueryIntent.UNKNOWN
                    )
                    sq_weights = weights_for_intent(sq_intent)
                    sq_weighted = [
                        (res, getattr(sq_weights, strat, 1.0)) for strat, res in sq_strategy_results.items() if res
                    ]
                    if not sq_weighted:
                        return []
                    if all(w == 1.0 for _, w in sq_weighted):
                        return rrf_fusion([r for r, _ in sq_weighted], k=self.rrf_k)
                    return weighted_rrf_fusion(sq_weighted, k=self.rrf_k)

                # sub_queries[0] is the original (already in fused); expand the rest
                sub_fused_lists = await asyncio.gather(*[_fuse_sub_query(sq) for sq in sub_queries[1:]])
                non_empty = [sf for sf in sub_fused_lists if sf]
                if non_empty:
                    fused = rrf_fusion([fused, *non_empty], k=self.rrf_k)

        # 4b. M10 chunk expansion — for each top-K hit with a chunk_id,
        # fetch sibling chunks (other vectors from the same SourceDocument)
        # and merge them into the candidate pool. Helps multi-hop / split-
        # evidence questions where the answer key is in chunk N±1 of a
        # chunk that hit. No-op when source_store is unwired or the flag
        # is off, or when the vector store doesn't expose
        # ``get_by_chunk_ids`` (older adapters degrade gracefully).
        if (
            self.source_chunk_expansion
            and self.source_store is not None
            and hasattr(self.vector_store, "get_by_chunk_ids")
        ):
            fused = await self._expand_via_sibling_chunks(fused, request.bank_id)

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
                chunk_id=getattr(item, "chunk_id", None),  # M10
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
                strategy_timings_ms=strategy_timings_ms or None,
                strategy_candidate_counts=strategy_candidate_counts or None,
            ),
            top_semantic_score=_top_semantic_score,
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
            hits = await self.vector_store.search_similar(query_vector, obs_bank, limit=limit, filters=obs_filters)
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
