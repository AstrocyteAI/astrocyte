from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from astrocyte.pipeline.cross_encoder_rerank import (
    cross_encoder_rerank,
)
from astrocyte.pipeline.embedding import generate_embeddings
from astrocyte.pipeline.fusion import (
    ScoredItem,
)
from astrocyte.pipeline.query_plan import build_query_plan
from astrocyte.pipeline.reflect import synthesize
from astrocyte.pipeline.reranking import (
    cross_encoder_like_rerank,
)
from astrocyte.policy.homeostasis import enforce_token_budget
from astrocyte.recall.authority import apply_recall_authority
from astrocyte.types import (
    MemoryHit,
    RecallRequest,
    ReflectRequest,
    ReflectResult,
    VectorItem,
)

if TYPE_CHECKING:
    pass



from astrocyte.pipeline._orchestrator_common import (
    _abstention_floor_for_skepticism,
    _resolve_skepticism_for_abstention,
    _source_ids_from_metadata,
)

_logger = logging.getLogger("astrocyte.mip")


class ReflectStageMixin:
    """Reflect pipeline: retrieve context → synthesize an answer.

    A behavior slice of PipelineOrchestrator, composed into it as a mixin.
    ``self`` is the full orchestrator at runtime (attributes come from its
    ``__init__`` / ``apply_config``); shared helpers live on sibling mixins.
    """

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
            tags=request.tags,
            as_of=request.as_of,
            query_reference_date=request.query_reference_date,
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
            tags=request.tags,
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

        # 2b. Auto-select a prompt when no MIP prompt override is set.  Priority
        # (highest → lowest):
        #   1. MIP explicit prompt (always wins — never overridden here)
        #   2. Evidence-strict gate: when top raw semantic score < threshold,
        #      retrieval is uncertain.  Force citation to prevent the LLM from
        #      constructing answers from tangential memories — the primary
        #      adversarial failure mode in open-domain benchmarks.  Uses cosine
        #      similarity from recall(), which is set to 0.0 when no semantic
        #      results were found.
        #   3. query_plan prompt variant (pre-retrieval query-shape routing)
        #   4. _auto_prompt_variant fallback (legacy temporal/inference heuristic)
        _EVIDENCE_STRICT_THRESHOLD = 0.5
        if mip_reflect is None or mip_reflect.prompt is None:
            from astrocyte.mip.schema import ReflectSpec
            from astrocyte.pipeline.reflect import _auto_prompt_variant

            prompt_variant = query_plan.prompt_variant or _auto_prompt_variant(request.query)

            # Evidence-strict override: weak retrieval scores mean the top
            # semantic match is only tangentially related — upgrade to citation
            # mode so the LLM admits uncertainty instead of hallucinating.
            # Skip if query_plan already chose evidence_strict (adversarial
            # query shape) — no need to double-set.
            if recall_result.top_semantic_score < _EVIDENCE_STRICT_THRESHOLD and prompt_variant != "evidence_strict":
                prompt_variant = "evidence_strict"

            if prompt_variant is not None:
                if mip_reflect is None:
                    mip_reflect = ReflectSpec(prompt=prompt_variant)
                else:
                    # Preserve any other MIP settings (e.g. promote_metadata)
                    mip_reflect = ReflectSpec(
                        prompt=prompt_variant,
                        promote_metadata=mip_reflect.promote_metadata,
                    )

        # 2b1. Adversarial-defense gate: premise verification.
        # Decompose the question into atomic claims, verify each
        # against memory. Short-circuit to "insufficient evidence"
        # when ANY presupposition fails. Targets false-premise and
        # negative-existence adversarial questions.
        if self.adversarial_premise_verification_enabled and self.llm_provider is not None:
            try:
                from astrocyte.pipeline.premise_verification import verify_question

                async def _premise_recall(claim: str, max_results: int) -> list[MemoryHit]:
                    sub_request = RecallRequest(
                        query=claim,
                        bank_id=request.bank_id,
                        max_results=max_results,
                        max_tokens=request.max_tokens,
                        tags=request.tags,
                        as_of=request.as_of,
                        query_reference_date=request.query_reference_date,
                    )
                    sub_result = await self.recall(sub_request)
                    return sub_result.hits

                verification = await verify_question(
                    request.query,
                    recall_fn=_premise_recall,
                    llm_provider=self.llm_provider,
                    min_confidence=self.adversarial_premise_min_confidence,
                )
                short_circuit = verification.short_circuit_message()
                if short_circuit is not None:
                    _logger.info(
                        "reflect: premise verification short-circuit — %s",
                        short_circuit,
                    )
                    return ReflectResult(answer=short_circuit, sources=[])
            except Exception as exc:
                _logger.warning(
                    "premise verification failed (%s); continuing without the guard.",
                    exc,
                )

        # 2c. Adversarial-defense gate: score-floor abstention.
        # Distinct from the evidence-strict prompt switch above.
        # ``evidence_strict`` keeps invoking the LLM with a tighter
        # prompt; the abstention floor short-circuits BEFORE any LLM
        # call when retrieval is so weak that the question almost
        # certainly has no answer in memory. Targets adversarial
        # questions (negative existence, false premise, time-shift)
        # where the LLM left to its own devices invents an answer.
        #
        # The effective floor is now derived from the request's
        # ``dispositions.skepticism`` (1=trust→never abstain,
        # 5=skeptical→aggressive abstention) rather than the legacy
        # global ``abstention_enabled`` bool. This lets one deployment
        # serve both adversarial-resistant agents and answer-everything
        # assistants from a single config — see
        # ``_abstention_floor_for_skepticism`` for the mapping. Legacy
        # ``abstention_enabled`` remains as a fallback when no
        # per-call dispositions are supplied.
        skepticism = _resolve_skepticism_for_abstention(
            request.dispositions,
            self.adversarial_abstention_enabled,
        )
        effective_floor = _abstention_floor_for_skepticism(
            skepticism,
            self.adversarial_abstention_floor,
        )
        if (
            effective_floor > 0.0
            and recall_result.top_semantic_score < effective_floor
            and (not recall_result.hits or all((h.score or 0.0) < effective_floor for h in recall_result.hits[:5]))
        ):
            _logger.info(
                "reflect: abstention floor triggered (top_semantic=%.3f < "
                "%.3f, skepticism=%d); returning 'insufficient evidence' "
                "without LLM call.",
                recall_result.top_semantic_score,
                effective_floor,
                skepticism,
            )
            return ReflectResult(
                answer="insufficient evidence: no memory supports this question.",
                sources=[],
            )

        # 3. Synthesize. Two paths:
        #
        # (a) Agentic loop (Hindsight parity) — when configured, the LLM
        #     selects between ``recall`` and ``done`` over up to N
        #     iterations. Targets multi-hop / open-domain where a
        #     single-shot recall misses bridge memories. Each loop
        #     ``recall`` reuses the full upgraded recall pipeline (RRF
        #     + spread + cross-encoder rerank + scope tags).
        #
        # (b) Single-shot synthesis — original path. Faster, simpler.
        synthesize_kwargs = dict(
            dispositions=request.dispositions,
            max_tokens=request.max_tokens or 2048,
            authority_context=auth_ctx,
            mip_reflect=mip_reflect,
            # Forward the relative-phrase anchor so the synthesis
            # prompt's ``<reference_date>`` block reflects the
            # question's contemporaneous date, not the run wall-clock.
            query_reference_date=request.query_reference_date,
        )
        if self.agentic_reflect_params is not None:
            from astrocyte.pipeline.agentic_reflect import agentic_reflect

            async def _loop_recall(query: str, max_results: int) -> list[MemoryHit]:
                # Reuses everything we just shipped: spread, cross-
                # encoder rerank, tag scoping, RRF — same recall the
                # outer step ran, parameterized by the agent's refined
                # query and its requested max_results.  Forwards the
                # outer ``as_of`` (audit replay) AND
                # ``query_reference_date`` (relative-phrase anchor) so
                # every sub-recall the agent makes sees the same
                # temporal context as the seed reflect.
                sub_request = RecallRequest(
                    query=query,
                    bank_id=request.bank_id,
                    max_results=max_results,
                    max_tokens=request.max_tokens,
                    tags=request.tags,
                    as_of=request.as_of,
                    query_reference_date=request.query_reference_date,
                )
                sub_result = await self.recall(sub_request)
                return sub_result.hits

            # ``search_observations`` tool — searches the consolidated
            # observation layer when the consolidator is wired up. The
            # ::obs bank scope is reused to mirror how observations are
            # stored at retain time.
            observations_fn = None
            if self._observation_consolidator is not None:

                async def _loop_observations(query: str, max_results: int) -> list[MemoryHit]:
                    qvec_batch = await generate_embeddings([query], self.llm_provider)
                    qvec = qvec_batch[0] if qvec_batch else []
                    # ``request.as_of`` (when set) propagates through to
                    # observation search so time-travel reflect sees the
                    # same observation snapshot as raw recall.  When
                    # unset, search uses the bank's current state.
                    obs_results = await self._retrieve_observations(
                        qvec,
                        request.bank_id,
                        max_results,
                        request.as_of,
                    )
                    # Convert ScoredItem → MemoryHit so the loop can
                    # cite IDs uniformly across tools.
                    return [
                        MemoryHit(
                            text=item.text,
                            score=item.score,
                            fact_type=item.fact_type,
                            metadata=item.metadata,
                            tags=item.tags,
                            memory_id=item.id,
                            bank_id=request.bank_id,
                            memory_layer="observation",
                        )
                        for item in (obs_results or [])
                    ]

                observations_fn = _loop_observations

            # ``expand`` tool — fetch source memories cited by a
            # compiled fact / wiki / observation. Reuses the existing
            # source-expansion path with a single-id seed.
            async def _loop_expand(memory_id: str, max_sources: int) -> list[MemoryHit]:
                # Find the seed hit in the running pool to expand from.
                seed: MemoryHit | None = next(
                    (h for h in recall_result.hits if h.memory_id == memory_id),
                    None,
                )
                if seed is None:
                    return []
                expanded = await self._expand_reflect_sources(
                    request.bank_id,
                    [seed],
                    limit=max(1, max_sources) + 1,
                    tags=request.tags,
                )
                # Drop the seed itself so the model sees only NEW evidence.
                return [h for h in expanded if h.memory_id != memory_id]

            # ``search_mental_models`` tool — top of the hierarchy.
            # Forwards to ``MentalModelStore.list(bank_id, scope=...)``,
            # converts each MentalModel to a MemoryHit so the agent can
            # cite ``model_id`` like any other ``memory_id``.  Returns
            # all models in scope (no semantic ranking — mental models
            # are usually a handful per bank, the LLM picks).
            mental_models_fn = None
            if self.mental_model_service is not None:

                async def _loop_mental_models(
                    query: str,
                    scope: str | None,
                ) -> list[MemoryHit]:
                    try:
                        models = await self.mental_model_service.list(  # type: ignore[union-attr]
                            request.bank_id,
                            scope=scope,
                        )
                    except Exception as exc:
                        _logger.warning(
                            "agentic_reflect.mental_models list failed (%s)",
                            exc,
                        )
                        return []
                    return [
                        MemoryHit(
                            text=f"# {m.title}\n\n{m.content}",
                            score=1.0,
                            fact_type="mental_model",
                            metadata={
                                "scope": m.scope,
                                "revision": m.revision,
                                "refreshed_at": m.refreshed_at.isoformat(),
                            },
                            memory_id=m.model_id,
                            bank_id=m.bank_id,
                            memory_layer="mental_model",
                        )
                        for m in models
                    ]

                mental_models_fn = _loop_mental_models

            return await agentic_reflect(
                request.query,
                initial_hits=recall_result.hits,
                recall_fn=_loop_recall,
                observations_fn=observations_fn,
                expand_fn=_loop_expand,
                mental_models_fn=mental_models_fn,
                llm_provider=self.llm_provider,
                params=self.agentic_reflect_params,
                final_synthesize_fn=synthesize,
                final_synthesize_kwargs=synthesize_kwargs,
            )
        return await synthesize(
            query=request.query,
            hits=recall_result.hits,
            llm_provider=self.llm_provider,
            **synthesize_kwargs,
        )

    def _rank_reflect_context(
        self,
        query: str,
        hits: list[MemoryHit],
        *,
        limit: int,
    ) -> list[MemoryHit]:
        """Apply final precision rerank and hierarchy before synthesis.

        Uses the configured cross-encoder reranker (Hindsight parity) when
        ``self.cross_encoder`` is set; otherwise falls back to the
        deterministic heuristic ``cross_encoder_like_rerank``. The
        heuristic remains the default so reflect stays dependency-free
        unless the operator opts into the cross-encoder backend.
        """
        if not hits:
            return hits

        items = [
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
        ]

        if self.cross_encoder is not None:
            try:
                scored = cross_encoder_rerank(
                    items,
                    query,
                    model=self.cross_encoder,
                    top_k=self.cross_encoder_top_k,
                )
            except Exception as exc:  # pragma: no cover — defensive
                _logger.warning(
                    "cross-encoder rerank failed (%s); falling back to heuristic.",
                    exc,
                )
                scored = cross_encoder_like_rerank(items, query)
        else:
            scored = cross_encoder_like_rerank(items, query)

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
        tags: list[str] | None = None,
    ) -> list[MemoryHit]:
        """Append raw sources cited by top wiki/observation hits.

        This mirrors Hindsight's reflect loop in a bounded, non-agentic form:
        start from compiled/observation evidence, then expand to raw facts for
        grounding before synthesis.

        ``tags`` (optional): when set, only fetched memories carrying every
        listed tag are appended. Closes the leak where a tag-scoped reflect
        could otherwise pull cross-scope raw memories via a wiki page's
        ``_wiki_source_ids`` metadata.
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

        raw_hits = await self._fetch_memory_hits_by_id(bank_id, source_ids, tags=tags)
        existing_ids = {h.memory_id for h in hits if h.memory_id}
        expanded = list(hits)
        for raw in raw_hits:
            if raw.memory_id and raw.memory_id not in existing_ids:
                expanded.append(raw)
                existing_ids.add(raw.memory_id)
            if len(expanded) >= limit:
                break
        return expanded[:limit]

    async def _fetch_memory_hits_by_id(
        self,
        bank_id: str,
        ids: list[str],
        *,
        tags: list[str] | None = None,
    ) -> list[MemoryHit]:
        target = set(ids)
        found: dict[str, VectorItem] = {}
        offset = 0
        batch = 100
        # Tag scoping: a fetched memory is kept only if it carries every
        # tag in ``tags``. ``None``/empty disables the filter (legacy
        # behavior). Comparison is case-insensitive to match recall's
        # tag-filter convention.
        required_tags = {str(t).lower() for t in tags} if tags else None
        while target:
            chunk = await self.vector_store.list_vectors(bank_id, offset=offset, limit=batch)
            if not chunk:
                break
            for item in chunk:
                if item.id not in target:
                    continue
                if required_tags is not None:
                    item_tags = {str(t).lower() for t in (item.tags or [])}
                    if not required_tags.issubset(item_tags):
                        # ID matches but scope tags missing — drop and
                        # mark as resolved so we don't keep scanning.
                        target.discard(item.id)
                        continue
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
