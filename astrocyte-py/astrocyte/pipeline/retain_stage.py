from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from astrocyte.config import ExtractionProfileConfig
from astrocyte.pipeline.chunking import chunk_text
from astrocyte.pipeline.embedding import generate_embeddings
from astrocyte.pipeline.entity_extraction import extract_entities
from astrocyte.pipeline.extraction import (
    merged_user_and_builtin_profiles,
    prepare_retain_input,
    resolve_retain_chunking,
)
from astrocyte.recall.authority import merge_retain_metadata_authority_tier
from astrocyte.types import (
    Entity,
    EntityLink,
    MemoryEntityAssociation,
    MemoryLink,
    RetainRequest,
    RetainResult,
    VectorItem,
)

if TYPE_CHECKING:
    pass



from astrocyte.pipeline._orchestrator_common import (
    _build_cooccurrence_pairs,
    _entities_from_metadata,
)

_logger = logging.getLogger("astrocyte.mip")


class RetainStageMixin:
    """Retain pipeline: chunk → extract → embed → persist (+ retain_many).

    A behavior slice of PipelineOrchestrator, composed into it as a mixin.
    ``self`` is the full orchestrator at runtime (attributes come from its
    ``__init__`` / ``apply_config``); shared helpers live on sibling mixins.
    """

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
        targets = [e for e in entities if e.embedding is None and e.name and e.name.strip()]
        if not targets:
            return
        try:
            vectors = await generate_embeddings(
                [e.name.strip() for e in targets],
                self.llm_provider,
            )
        except Exception as exc:
            _logger.warning(
                "entity name embedding failed (resolver will fall back to trigram-only tier): %s",
                exc,
            )
            return
        for entity, vec in zip(targets, vectors, strict=False):
            entity.embedding = vec

    async def _structured_fact_extraction_for_text(
        self,
        prepared,
        request: RetainRequest,
        *,
        chunk_strategy: str = "paragraph",
        chunk_max_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> tuple[
        list[str] | None,
        list[Entity] | None,
        list[tuple[int, str]] | None,  # (entity_idx_in_full_list, memory_idx) associations
        list[tuple[int, int, float]] | None,  # (source_memory_idx, target_memory_idx, confidence) caused_by
    ]:
        """Run the structured 5-dim fact extraction path.

        Returns ``(fact_texts, entities, associations, caused_by)`` when
        the path is enabled and produces facts; ``(None, None, None, None)``
        otherwise. Caller falls through to the legacy chunk_text +
        extract_entities path when None is returned.

        Indices in ``associations`` and ``caused_by`` reference positions
        in the returned ``fact_texts``/``entities`` lists. Callers
        resolve them to memory IDs after store_vectors.
        """
        # SFE supersedes the legacy ``prepared.extract_entities`` gate.
        # The legacy setting controlled which entity-extraction PATH
        # ran (metadata vs LLM); SFE is a third path that produces
        # both entities and structured facts in one call. When SFE is
        # enabled, it fires regardless of the profile's entity
        # extraction mode — the profile's metadata-entities are
        # ignored in favor of the richer SFE output.
        if not self.structured_fact_extraction_enabled or self.llm_provider is None:
            return None, None, None, None

        try:
            # Pre-chunk using the SAME strategy the legacy retain path
            # would use (resolved from the extraction profile), then ask
            # the LLM to enrich each chunk with metadata WITHOUT
            # paraphrasing. Stored memory text = original chunk
            # vocabulary at the granularity the profile specifies
            # (e.g. dialogue turns for conversations, paragraphs for
            # documents).
            #
            # The legacy "concise" path (``extract_facts``) was removed
            # in M9 — it caused severe recall_hit_rate degradation
            # because the LLM-paraphrased ``what`` field lost the
            # surface vocabulary that question embeddings share. Verbatim
            # is now the only supported mode; ``validate_astrocyte_config``
            # rejects ``extraction_mode: concise`` at config load.
            #
            # Open-domain regression observed on 2026-05-02 traced to
            # hardcoded "paragraph" strategy producing one giant chunk
            # per LoCoMo session (no paragraph breaks in dialogue text).
            # Profile-driven strategy fixes that.
            from astrocyte.pipeline.chunking import chunk_text
            from astrocyte.pipeline.fact_extraction import (
                extract_facts_verbatim,
                extract_facts_verbatim_parallel,
                materialize_facts,
            )

            pre_chunk_kwargs: dict[str, int] = {}
            if chunk_max_size is not None:
                pre_chunk_kwargs["max_chunk_size"] = chunk_max_size
            if chunk_overlap is not None:
                pre_chunk_kwargs["overlap"] = chunk_overlap
            chunks_local = chunk_text(
                prepared.text,
                strategy=chunk_strategy,
                **pre_chunk_kwargs,
            )
            # Phase 3: route to the per-chunk parallel path when opt-in.
            # Drops cross-chunk causal_relations — pair with
            # ``causal_links.enabled=false``.
            if self.structured_fact_extraction_parallel_chunks:
                facts = await extract_facts_verbatim_parallel(
                    chunks_local,
                    self.llm_provider,
                    event_date=request.occurred_at,
                    max_concurrency=(self.structured_fact_extraction_parallel_chunks_max_concurrency),
                )
            else:
                facts = await extract_facts_verbatim(
                    chunks_local,
                    self.llm_provider,
                    event_date=request.occurred_at,
                )
        except Exception as exc:
            _logger.warning(
                "structured fact extraction failed (%s); falling back to legacy chunk + entity-extraction path.",
                exc,
            )
            return None, None, None, None
        if not facts:
            return None, None, None, None

        # Materialize without embeddings here — embeddings are batched
        # later for cost. We only need the list of fact texts and the
        # pre-extracted entities + association index map.
        materialized = materialize_facts(
            facts,
            bank_id=request.bank_id,
            occurred_at=request.occurred_at,
        )
        fact_texts = [item.text for item in materialized.vector_items]
        entities = list(materialized.entities)

        # Build (memory_idx, entity_idx) pairs from the materialized
        # association tuples (which reference items by ID). We need
        # indices because the caller hasn't assigned final memory IDs
        # yet — those come after dedup + store_vectors.
        item_id_to_idx = {item.id: i for i, item in enumerate(materialized.vector_items)}
        ent_id_to_idx = {e.id: i for i, e in enumerate(entities)}
        associations: list[tuple[int, str]] = []
        for item_id, ent_id in materialized.memory_entity_associations:
            mem_idx = item_id_to_idx.get(item_id)
            ent_idx = ent_id_to_idx.get(ent_id)
            if mem_idx is None or ent_idx is None:
                continue
            associations.append((ent_idx, mem_idx))

        # caused_by edges: indices both into fact list (= memory list).
        caused_by: list[tuple[int, int, float]] = []
        for link in materialized.memory_links:
            src_idx = item_id_to_idx.get(link.source_memory_id)
            tgt_idx = item_id_to_idx.get(link.target_memory_id)
            if src_idx is None or tgt_idx is None:
                continue
            caused_by.append((src_idx, tgt_idx, float(link.confidence)))

        return fact_texts, entities, associations, caused_by

    async def _persist_semantic_links(
        self,
        bank_id: str,
        memory_ids: list[str],
        embeddings: list[list[float]],
    ) -> None:
        """Compute & persist semantic-kNN edges for a freshly-stored batch.

        Hindsight parity (C3a): each new memory gets ``MemoryLink(
        link_type="semantic", weight=cosine)`` edges to its top-K
        most-similar existing memories with similarity ≥ threshold.
        Best-effort — failures log and continue rather than aborting
        retain.
        """
        if not self.semantic_link_graph_enabled or self.graph_store is None or not memory_ids:
            return
        try:
            from astrocyte.pipeline.semantic_link_graph import compute_semantic_links

            links = await compute_semantic_links(
                bank_id=bank_id,
                new_memory_ids=memory_ids,
                new_embeddings=embeddings,
                vector_store=self.vector_store,
                top_k=self.semantic_link_graph_top_k,
                similarity_threshold=self.semantic_link_graph_threshold,
            )
        except Exception as exc:
            _logger.warning("semantic_link_graph computation failed: %s", exc)
            return
        if not links:
            return
        try:
            await self.graph_store.store_memory_links(links, bank_id)
        except Exception as exc:
            _logger.warning("storing semantic memory_links failed: %s", exc)

    async def _process_record_entities_with_retry(
        self,
        record: dict[str, Any],
        *,
        max_retries: int = 10,
        base_delay: float = 0.1,
    ) -> None:
        """Retry-wrapper around :meth:`_process_record_entities`.

        AGE's entity ``MERGE`` path takes per-label advisory locks
        (``pg_advisory_xact_lock(label_oid)``) so concurrent retains
        racing on the same label name don't produce duplicate label
        rows. Three or more retains contending on the same label can
        produce a CYCLIC wait: P1 holds advisory, waits on tx; P2 waits
        on advisory; P3 holds tx, waits on advisory — Postgres breaks
        the cycle by aborting one of them with ``DeadlockDetected``.

        This is timing-sensitive: with HNSW vector inserts the retain
        latency was high enough that the cyclic pattern was rare in
        practice. With DiskANN's faster inserts (2026-05-06 default
        switch) the LoCoMo bench surfaced it within 20 retains.
        Postgres-style deadlocks are inherently retriable — only the
        loser's transaction rolls back, so we just try again with
        exponential backoff.

        Tuning history:

        - 2026-05-06 (initial): ``max_retries=3``, ``base_delay=0.2``.
          Absorbed most deadlocks but the LoCoMo bench surfaced 4-way
          cyclic waits that the 3 attempts couldn't outlast.
        - 2026-05-06 (revised): ``max_retries=10``, ``base_delay=0.1``.
          Retain throughput evidence: with ``concurrency=10`` retains
          and a per-bank entity overlap rate around 30% (LoCoMo
          characters appearing in many sessions), deadlocks fire on
          ~5% of retains. 10 retries with 0.1s × 2^n backoff
          (capped at 3.6s by the worst case) gives 51.1s max stall —
          ample headroom for the cyclic-wait probability tail. The
          bench earlier observed 6 deadlocks absorbed before one
          exhausted — bumping to 10 attempts more than doubles the
          probability mass we cover.
        """
        last_exc: BaseException | None = None
        for attempt in range(max_retries):
            try:
                await self._process_record_entities(record)
                return
            except Exception as exc:  # noqa: BLE001 — match psycopg without import dep
                # Match by class name + message so we don't need a hard
                # dependency on psycopg in this module (the orchestrator
                # is provider-agnostic; deadlocks are a Postgres-side
                # concept that bubbles up via whatever DB driver the
                # graph_store happens to use).
                name = type(exc).__name__.lower()
                msg = str(exc).lower()
                is_deadlock = "deadlock" in name or "deadlock detected" in msg
                if not is_deadlock or attempt == max_retries - 1:
                    raise
                last_exc = exc
                delay = base_delay * (2**attempt)
                _logger.warning(
                    "_process_record_entities deadlock on attempt %d/%d (%s); retrying in %.2fs",
                    attempt + 1,
                    max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        # Defensive — loop body either returns or re-raises; this is
        # only reachable if max_retries=0 (which we guard against above
        # via the attempt < max_retries-1 check).
        if last_exc is not None:
            raise last_exc

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
            async with self._profiler.time("entity_emb"):
                await self._attach_entity_name_embeddings(entities)
            # Path B (Hindsight-style): rewrite tentative IDs to canonical
            # IDs BEFORE storage so different surface forms that match an
            # existing canonical never produce duplicate entity rows. The
            # post-store ``resolve()`` alias pass becomes redundant in this
            # mode and is skipped below.
            if self.entity_resolver.canonical_resolution:
                try:
                    async with self._profiler.time("entity_resolve"):
                        await self.entity_resolver.resolve_canonical_ids_in_place(
                            new_entities=entities,
                            bank_id=request.bank_id,
                            graph_store=self.graph_store,
                            event_date=request.occurred_at,
                        )
                except Exception as exc:
                    _logger.warning(
                        "canonical resolution failed during retain (falling back to tentative IDs): %s",
                        exc,
                    )

        async with self._profiler.time("entity_store"):
            entity_ids = await self.graph_store.store_entities(entities, request.bank_id)

        # Per-fact entity associations when SFE supplied them
        # (each memory linked only to entities IT mentions); legacy path
        # uses the Cartesian product.
        sfe_associations = record.get("sfe_associations")
        if sfe_associations is not None:
            associations = [
                MemoryEntityAssociation(
                    memory_id=memory_ids[mem_idx],
                    entity_id=entity_ids[ent_idx],
                )
                for ent_idx, mem_idx in sfe_associations
                if 0 <= ent_idx < len(entity_ids) and 0 <= mem_idx < len(memory_ids)
            ]
        else:
            associations = [
                MemoryEntityAssociation(memory_id=mid, entity_id=eid) for mid in memory_ids for eid in entity_ids
            ]
        async with self._profiler.time("entity_link_mem"):
            await self.graph_store.link_memories_to_entities(associations, request.bank_id)
        # Same cap as the single-record retain path — see comment there.
        # Without it the all-pairs Cartesian product dominates retain
        # wall on entity-dense workloads (LME profile 2026-05-06).
        if self.entity_cooccurrence_enabled:
            pairs = _build_cooccurrence_pairs(
                entity_ids,
                self.entity_cooccurrence_max_entities,
            )
            if pairs:
                links = [EntityLink(entity_a=a, entity_b=b, link_type="co_occurs") for a, b in pairs]
                async with self._profiler.time("entity_co_occur"):
                    await self.graph_store.store_links(links, request.bank_id)

        # MemoryLinks (caused_by). Two paths:
        # - SFE supplied them inline → resolve indices, store.
        # - Legacy fact_causal_extraction LLM call per record.
        sfe_caused_by = record.get("sfe_caused_by")
        chunks: list[str] = record.get("chunks") or []
        if sfe_caused_by:
            memory_links = [
                MemoryLink(
                    source_memory_id=memory_ids[src_idx],
                    target_memory_id=memory_ids[tgt_idx],
                    link_type="caused_by",
                    confidence=conf,
                    weight=1.0,
                    created_at=datetime.now(timezone.utc),
                    metadata={"bank_id": request.bank_id, "source": "fact_extraction"},
                )
                for src_idx, tgt_idx, conf in sfe_caused_by
                if 0 <= src_idx < len(memory_ids) and 0 <= tgt_idx < len(memory_ids) and src_idx != tgt_idx
            ]
            if memory_links:
                try:
                    await self.graph_store.store_memory_links(
                        memory_links,
                        request.bank_id,
                    )
                except Exception as exc:
                    _logger.warning("storing memory_links failed: %s", exc)
        elif (
            self.causal_links_enabled
            and len(memory_ids) > 1
            and len(chunks) == len(memory_ids)
            and self.llm_provider is not None
        ):
            try:
                from astrocyte.pipeline.fact_causal_extraction import (
                    build_memory_links_from_relations,
                    extract_fact_causal_relations,
                )

                relations = await extract_fact_causal_relations(
                    chunks,
                    self.llm_provider,
                    max_pairs_per_fact=self.causal_max_pairs_per_memory,
                    min_confidence=self.causal_min_confidence,
                )
                memory_links = build_memory_links_from_relations(
                    relations,
                    memory_ids,
                    bank_id=request.bank_id,
                )
            except Exception as exc:
                _logger.warning("fact-level causal extraction failed: %s", exc)
                memory_links = []
            if memory_links:
                try:
                    await self.graph_store.store_memory_links(
                        memory_links,
                        request.bank_id,
                    )
                except Exception as exc:
                    _logger.warning("storing memory_links failed: %s", exc)

        # Legacy two-stage flow: post-store alias resolution. Skipped when
        # ``canonical_resolution=True`` because IDs are already canonical.
        if self.entity_resolver is not None and not self.entity_resolver.canonical_resolution:
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

    async def _provision_source_provenance(
        self,
        request: RetainRequest,
        prepared_text: str,
        chunks: list[str],
    ) -> list[str | None]:
        """Stamp the (document, chunks) pair into the SourceStore.

        Returns the per-chunk ``chunk_id`` list, parallel to ``chunks``.
        On failure (or when the SourceStore is not configured / the flag
        is off), returns ``[None] * len(chunks)`` so retain continues
        with anonymous vectors — provenance must never break ingest.

        The document id is deterministic (SHA-256 of the prepared text,
        prefixed with ``doc:``) so the same input ingested twice resolves
        to the same SourceDocument and the dedup probe is hit. Chunk ids
        are ``{document_id}:{i}`` for the same reason.
        """
        n = len(chunks)
        empty: list[str | None] = [None] * n
        if self.source_store is None or not self.source_retain_provenance:
            return empty

        # Late import: keeps the module import-cost low and avoids a hard
        # dependency on the M10 types from the orchestrator's surface area.
        from astrocyte.types import SourceChunk, SourceDocument

        try:
            doc_hash = hashlib.sha256(prepared_text.encode("utf-8")).hexdigest()
            doc_id_default = f"doc:{doc_hash[:16]}"
            document = SourceDocument(
                id=doc_id_default,
                bank_id=request.bank_id,
                title=None,
                source_uri=request.source,
                content_hash=doc_hash,
                content_type=request.content_type,
                metadata=None,
            )
            # store_document is idempotent on (bank_id, content_hash); the
            # returned id may differ from doc_id_default if a prior live
            # row with the same hash already exists.
            stored_doc_id = await self.source_store.store_document(document)  # type: ignore[union-attr]

            source_chunks: list[SourceChunk] = []
            for i, chunk_text_str in enumerate(chunks):
                chunk_hash = hashlib.sha256(chunk_text_str.encode("utf-8")).hexdigest()
                source_chunks.append(
                    SourceChunk(
                        id=f"{stored_doc_id}:{i}",
                        bank_id=request.bank_id,
                        document_id=stored_doc_id,
                        chunk_index=i,
                        text=chunk_text_str,
                        content_hash=chunk_hash,
                        metadata=None,
                    )
                )
            chunk_ids = await self.source_store.store_chunks(source_chunks)  # type: ignore[union-attr]
            if len(chunk_ids) != n:
                # Should not happen for well-behaved adapters; defend
                # against shape mismatch by falling back rather than
                # silently misaligning chunk_ids onto VectorItems.
                _logger.warning(
                    "source_store.store_chunks returned %d ids, expected %d — falling back",
                    len(chunk_ids),
                    n,
                )
                return empty
            return list(chunk_ids)
        except Exception as exc:
            # Defensive: provenance is best-effort. Log and degrade to
            # anonymous vectors so a SourceStore outage doesn't break ingest.
            _logger.warning("source-aware retain failed; continuing without provenance: %s", exc)
            return empty

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

        # Structured 5-dim fact extraction (opt-in). Replaces chunking +
        # entity extraction with a single LLM call producing facts.
        # Falls back to legacy chunk_text + extract_entities when
        # disabled or when extraction returns no facts.
        # SFE uses its OWN chunking strategy (not the profile's), because
        # SFE has different granularity needs: large chunks give the LLM
        # full context for metadata extraction. Default "paragraph" wins
        # +2.5pts on LoCoMo over the profile-driven "dialogue" choice.
        async with self._profiler.time("sfe"):
            (
                sfe_fact_texts,
                sfe_entities,
                sfe_associations,
                sfe_caused_by,
            ) = await self._structured_fact_extraction_for_text(
                prepared,
                request,
                chunk_strategy=self.structured_fact_extraction_chunk_strategy,
                chunk_max_size=(
                    self.structured_fact_extraction_chunk_max_size
                    if self.structured_fact_extraction_chunk_max_size is not None
                    else chunking.max_size
                ),
                chunk_overlap=chunking.overlap,
            )
        if sfe_fact_texts is not None:
            chunks = list(sfe_fact_texts)
        else:
            chunks = chunk_text(prepared.text, strategy=chunking.strategy, **chunk_kwargs)
        if not chunks:
            return RetainResult(stored=False, error="No content after chunking")

        # 2. Generate embeddings for all chunks
        async with self._profiler.time("embed"):
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
                request.bank_id,
                emb,
                threshold_override=dedup_threshold_override,
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

        # Remap structured-fact-extraction indices through dedup. The
        # association and caused_by lists referenced positions in the
        # ORIGINAL chunks; after dedup, indices need remapping to the
        # surviving positions (or the records dropped entirely).
        if sfe_associations is not None or sfe_caused_by is not None:
            old_to_new = {old: new for new, old in enumerate(keep_indices)}
            if sfe_associations is not None:
                sfe_associations = [
                    (ent_idx, old_to_new[mem_idx]) for ent_idx, mem_idx in sfe_associations if mem_idx in old_to_new
                ]
            if sfe_caused_by is not None:
                sfe_caused_by = [
                    (old_to_new[src], old_to_new[tgt], conf)
                    for src, tgt, conf in sfe_caused_by
                    if src in old_to_new and tgt in old_to_new
                ]

        # 3. Extract entities (profile can disable; metadata profiles avoid LLM calls).
        # Structured fact extraction provides entities pre-extracted —
        # skip the second LLM call when it ran successfully.
        if sfe_entities is not None:
            entities = list(sfe_entities)
        elif profile is not None and profile.entity_extraction == "metadata":
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
            "_created_at",
            datetime.now(timezone.utc).isoformat(),
        )

        # 3b. M10: persist source-document + chunk provenance, get back per-chunk
        # ``chunk_id``s that we'll stamp onto each VectorItem so recall can
        # later resolve "which document/chunk did this memory come from".
        # Returns ``[None] * len(chunks)`` when source_store is not configured
        # or the flag is off — fully backward-compatible.
        chunk_ids = await self._provision_source_provenance(
            request,
            prepared.text,
            chunks,
        )

        # 4. Store vectors
        memory_ids: list[str] = []
        items: list[VectorItem] = []
        for chunk, embedding, chunk_id in zip(chunks, embeddings, chunk_ids):
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
                    chunk_id=chunk_id,  # M10: source-chunk backreference
                )
            )

        async with self._profiler.time("store_vec"):
            await self.vector_store.store_vectors(items)

        # Hindsight-parity semantic-kNN graph (C3a). Best-effort: links
        # the new memories to their nearest existing neighbors so the
        # link-expansion retrieval CTE has a precomputed semantic
        # signal at recall time. Skipped when disabled.
        await self._persist_semantic_links(
            request.bank_id,
            [item.id for item in items],
            [item.vector for item in items],
        )

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
                        item.id,
                        exc,
                    )

        # 5. Store entities and links (if graph store configured)
        if self.graph_store and entities:
            # 5a. Attach name embeddings before persistence so the
            # Hindsight-inspired entity-resolution cascade has the cheap
            # cosine-similarity tier available.  Only fires when an
            # entity_resolver is wired up — without one, the embedding
            # would be persisted but never used, wasting API budget.
            if self.entity_resolver is not None:
                async with self._profiler.time("entity_emb"):
                    await self._attach_entity_name_embeddings(entities)
                # Path B (Hindsight): rewrite tentative IDs to canonicals
                # before storage. Skipped when canonical_resolution=False
                # to preserve the legacy two-stage (store → resolve aliases)
                # flow.
                if self.entity_resolver.canonical_resolution:
                    try:
                        async with self._profiler.time("entity_resolve"):
                            await self.entity_resolver.resolve_canonical_ids_in_place(
                                new_entities=entities,
                                bank_id=request.bank_id,
                                graph_store=self.graph_store,
                                event_date=request.occurred_at,
                            )
                    except Exception as exc:
                        _logger.warning(
                            "canonical resolution failed during retain (falling back to tentative IDs): %s",
                            exc,
                        )

            async with self._profiler.time("entity_store"):
                entity_ids = await self.graph_store.store_entities(entities, request.bank_id)

            # Link memories to entities. Two paths:
            # - Structured fact extraction provides per-fact-per-entity
            #   associations (each memory linked only to entities IT mentions),
            #   matching Hindsight's fact-grained granularity.
            # - Legacy path uses the Cartesian product (every memory linked
            #   to every entity in the batch), which works when extraction
            #   was over the whole text rather than per fact.
            if sfe_associations is not None:
                associations = [
                    MemoryEntityAssociation(
                        memory_id=memory_ids[mem_idx],
                        entity_id=entity_ids[ent_idx],
                    )
                    for ent_idx, mem_idx in sfe_associations
                    if 0 <= ent_idx < len(entity_ids) and 0 <= mem_idx < len(memory_ids)
                ]
            else:
                associations = [
                    MemoryEntityAssociation(memory_id=mid, entity_id=eid) for mid in memory_ids for eid in entity_ids
                ]
            async with self._profiler.time("entity_link_mem"):
                await self.graph_store.link_memories_to_entities(associations, request.bank_id)

            # Create co-occurrence links between entities. The 2026-05-06
            # LME retain-profile measured the unbounded all-pairs
            # Cartesian product at 34% of retain wall (52s tail at the
            # session-100 mark). The cap in
            # ``_build_cooccurrence_pairs`` bounds the work to O(K²) per
            # retain regardless of corpus size.
            if self.entity_cooccurrence_enabled:
                pairs = _build_cooccurrence_pairs(
                    entity_ids,
                    self.entity_cooccurrence_max_entities,
                )
                if pairs:
                    links = [EntityLink(entity_a=a, entity_b=b, link_type="co_occurs") for a, b in pairs]
                    async with self._profiler.time("entity_co_occur"):
                        await self.graph_store.store_links(links, request.bank_id)

            # Causal MemoryLinks. Two paths:
            # - Structured fact extraction supplies them inline (no
            #   extra LLM call); resolve indices to memory IDs and store.
            # - Legacy path runs a separate fact_causal_extraction LLM call.
            if sfe_caused_by is not None and sfe_caused_by:
                memory_links = [
                    MemoryLink(
                        source_memory_id=memory_ids[src_idx],
                        target_memory_id=memory_ids[tgt_idx],
                        link_type="caused_by",
                        confidence=conf,
                        weight=1.0,
                        created_at=datetime.now(timezone.utc),
                        metadata={"bank_id": request.bank_id, "source": "fact_extraction"},
                    )
                    for src_idx, tgt_idx, conf in sfe_caused_by
                    if 0 <= src_idx < len(memory_ids) and 0 <= tgt_idx < len(memory_ids) and src_idx != tgt_idx
                ]
                if memory_links:
                    try:
                        await self.graph_store.store_memory_links(
                            memory_links,
                            request.bank_id,
                        )
                    except Exception as exc:
                        _logger.warning("storing memory_links failed: %s", exc)
            elif (
                self.causal_links_enabled
                and len(memory_ids) > 1
                and len(chunks) == len(memory_ids)
                and self.llm_provider is not None
            ):
                # Legacy fact-causal-extraction LLM pass (separate call).
                try:
                    from astrocyte.pipeline.fact_causal_extraction import (
                        build_memory_links_from_relations,
                        extract_fact_causal_relations,
                    )

                    relations = await extract_fact_causal_relations(
                        chunks,
                        self.llm_provider,
                        max_pairs_per_fact=self.causal_max_pairs_per_memory,
                        min_confidence=self.causal_min_confidence,
                    )
                    memory_links = build_memory_links_from_relations(
                        relations,
                        memory_ids,
                        bank_id=request.bank_id,
                    )
                except Exception as exc:
                    _logger.warning("fact-level causal extraction failed: %s", exc)
                    memory_links = []
                if memory_links:
                    try:
                        await self.graph_store.store_memory_links(
                            memory_links,
                            request.bank_id,
                        )
                    except Exception as exc:
                        _logger.warning("storing memory_links failed: %s", exc)

            # 5b. Entity resolution (M11) — Hindsight-inspired tiered cascade.
            # Skipped when canonical_resolution=True (Path B) because IDs
            # are already canonical from the pre-store pass above. Otherwise
            # the legacy two-stage path runs the cascade and writes
            # alias_of links for matched candidates.
            if self.entity_resolver is not None and not self.entity_resolver.canonical_resolution:
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
                        scope="|".join(sorted(request.tags)) if request.tags else None,
                    )
                except Exception as exc:
                    _logger.warning("Observation consolidation task failed for bank %s: %s", bank_id, exc)

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

            # Structured 5-dim fact extraction (opt-in) — same branch as
            # retain(); produces fact texts + pre-extracted entities +
            # caused_by index pairs for this record.
            # SFE uses its own chunking strategy (see retain() above).
            async with self._profiler.time("sfe"):
                (
                    sfe_fact_texts,
                    sfe_entities,
                    sfe_associations,
                    sfe_caused_by,
                ) = await self._structured_fact_extraction_for_text(
                    prepared,
                    request,
                    chunk_strategy=self.structured_fact_extraction_chunk_strategy,
                    chunk_max_size=(
                        self.structured_fact_extraction_chunk_max_size
                        if self.structured_fact_extraction_chunk_max_size is not None
                        else chunking.max_size
                    ),
                    chunk_overlap=chunking.overlap,
                )
            if sfe_fact_texts is not None:
                chunks = list(sfe_fact_texts)
            else:
                chunks = chunk_text(prepared.text, strategy=chunking.strategy, **chunk_kwargs)
            if not chunks:
                results[index] = RetainResult(stored=False, error="No content after chunking")
                continue

            start = len(all_chunks)
            all_chunks.extend(chunks)
            records.append(
                {
                    "index": index,
                    "request": request,
                    "profile": profile,
                    "prepared": prepared,
                    "chunks": chunks,
                    "start": start,
                    "end": len(all_chunks),
                    # Carry the SFE artefacts through to the second loop and
                    # _process_record_entities. None entries mean the record
                    # took the legacy chunk_text + extract_entities path.
                    "sfe_entities": sfe_entities,
                    "sfe_associations": sfe_associations,
                    "sfe_caused_by": sfe_caused_by,
                }
            )

        if not records:
            return [result or RetainResult(stored=False, error="No content after chunking") for result in results]

        async with self._profiler.time("embed"):
            all_embeddings = await generate_embeddings(all_chunks, self.llm_provider)
        stored_records: list[dict[str, Any]] = []
        all_items: list[VectorItem] = []

        for record in records:
            request: RetainRequest = record["request"]
            profile: ExtractionProfileConfig | None = record["profile"]
            prepared = record["prepared"]
            chunks = record["chunks"]
            embeddings = all_embeddings[record["start"] : record["end"]]
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

            # Remap SFE indices through dedup (mirrors retain() path).
            sfe_associations = record["sfe_associations"]
            sfe_caused_by = record["sfe_caused_by"]
            sfe_entities = record["sfe_entities"]
            if sfe_associations is not None or sfe_caused_by is not None:
                old_to_new = {old: new for new, old in enumerate(keep_indices)}
                if sfe_associations is not None:
                    sfe_associations = [
                        (ent_idx, old_to_new[mem_idx]) for ent_idx, mem_idx in sfe_associations if mem_idx in old_to_new
                    ]
                if sfe_caused_by is not None:
                    sfe_caused_by = [
                        (old_to_new[src], old_to_new[tgt], conf)
                        for src, tgt, conf in sfe_caused_by
                        if src in old_to_new and tgt in old_to_new
                    ]
                # Persist remapped versions for _process_record_entities.
                record["sfe_associations"] = sfe_associations
                record["sfe_caused_by"] = sfe_caused_by

            if sfe_entities is not None:
                entities = list(sfe_entities)
            elif profile is not None and profile.entity_extraction == "metadata":
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

            # M10 source-aware retain — same helper as the single-retain path.
            chunk_ids = await self._provision_source_provenance(
                request,
                prepared.text,
                chunks,
            )

            memory_ids: list[str] = []
            items: list[VectorItem] = []
            for chunk, embedding, chunk_id in zip(chunks, embeddings, chunk_ids, strict=False):
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
                        chunk_id=chunk_id,  # M10: source-chunk backreference
                    )
                )

            record.update(
                {
                    "chunks": chunks,
                    "embeddings": embeddings,
                    "entities": entities,
                    "memory_ids": memory_ids,
                    "items": items,
                }
            )
            stored_records.append(record)
            all_items.extend(items)

        if all_items:
            async with self._profiler.time("store_vec"):
                await self.vector_store.store_vectors(all_items)

        # Hindsight-parity semantic-kNN graph (C3a) — same call as the
        # single-retain path, applied per record so each batch's
        # neighbors-search is scoped to its own bank.
        for record in stored_records:
            await self._persist_semantic_links(
                record["request"].bank_id,
                record["memory_ids"],
                record["embeddings"],
            )

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
                await asyncio.gather(*[self._process_record_entities_with_retry(r) for r in entity_records])

        for record in stored_records:
            request: RetainRequest = record["request"]
            memory_ids: list[str] = record["memory_ids"]
            embeddings: list[list[float]] = record["embeddings"]
            chunks: list[str] = record["chunks"]

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
                            scope="|".join(sorted(request.tags)) if request.tags else None,
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
