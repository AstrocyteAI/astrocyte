"""Pipeline orchestrator — coordinates Tier 1 retain/recall/reflect flows.

Async (coordinates I/O stages). See docs/_design/built-in-pipeline.md.
"""

from __future__ import annotations

import uuid
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
from astrocyte.pipeline.fusion import memory_hits_as_scored, rrf_fusion
from astrocyte.pipeline.reflect import synthesize
from astrocyte.pipeline.reranking import basic_rerank
from astrocyte.pipeline.retrieval import parallel_retrieve
from astrocyte.policy.homeostasis import enforce_token_budget
from astrocyte.policy.signal_quality import DedupDetector
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
    from astrocyte.provider import DocumentStore, GraphStore, LLMProvider, VectorStore


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
        max_chunk_size: int = 512,
        rrf_k: int = 60,
        semantic_overfetch: int = 5,
        extraction_profiles: dict[str, ExtractionProfileConfig] | None = None,
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
        self._dedup = DedupDetector(similarity_threshold=0.95)

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
        prepared = prepare_retain_input(
            request,
            profile,
            graph_store_configured=self.graph_store is not None,
        )
        strategy, max_chunk = resolve_retain_chunking(
            prepared.effective_content_type,
            profile=profile,
            default_strategy=self.chunk_strategy,
            default_max_chunk_size=self.max_chunk_size,
        )
        chunks = chunk_text(prepared.text, strategy=strategy, max_chunk_size=max_chunk)
        if not chunks:
            return RetainResult(stored=False, error="No content after chunking")

        # 2. Generate embeddings for all chunks
        embeddings = await generate_embeddings(chunks, self.llm_provider)

        # 2b. Per-chunk dedup — skip individual duplicate chunks, store the rest
        keep_indices: list[int] = []
        for i, emb in enumerate(embeddings):
            is_dup, _sim = self._dedup.is_duplicate(request.bank_id, emb)
            if not is_dup:
                keep_indices.append(i)

        if not keep_indices:
            return RetainResult(stored=False, deduplicated=True, error="All chunks are near-duplicates")

        chunks = [chunks[i] for i in keep_indices]
        embeddings = [embeddings[i] for i in keep_indices]

        # 3. Extract entities (profile can disable; requires graph store)
        entities = await extract_entities(prepared.text, self.llm_provider) if prepared.extract_entities else []

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
                    metadata=prepared.metadata,
                    tags=prepared.tags,
                    fact_type=prepared.fact_type,
                    occurred_at=request.occurred_at,
                )
            )

        await self.vector_store.store_vectors(items)

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
        )

        # 4. RRF fusion (local strategies + optional federated / manual external_context)
        ranked_lists = [results for results in strategy_results.values() if results]
        if request.external_context:
            ranked_lists.append(memory_hits_as_scored(request.external_context))
        fused = rrf_fusion(ranked_lists, k=self.rrf_k)

        # 5. Reranking
        reranked = basic_rerank(fused, request.query)

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

        # 2. Synthesize via LLM
        return await synthesize(
            query=request.query,
            hits=recall_result.hits,
            llm_provider=self.llm_provider,
            dispositions=request.dispositions,
            max_tokens=request.max_tokens or 2048,
        )
