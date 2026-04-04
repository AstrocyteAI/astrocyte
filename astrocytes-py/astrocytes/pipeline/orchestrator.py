"""Pipeline orchestrator — coordinates Tier 1 retain/recall/reflect flows.

Async (coordinates I/O stages). See docs/_design/built-in-pipeline.md.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from astrocytes.pipeline.chunking import chunk_text
from astrocytes.pipeline.embedding import generate_embeddings
from astrocytes.pipeline.entity_extraction import extract_entities
from astrocytes.pipeline.fusion import rrf_fusion
from astrocytes.pipeline.reflect import synthesize
from astrocytes.pipeline.reranking import basic_rerank
from astrocytes.pipeline.retrieval import parallel_retrieve
from astrocytes.policy.homeostasis import enforce_token_budget
from astrocytes.policy.signal_quality import DedupDetector
from astrocytes.types import (
    EntityLink,
    MemoryEntityAssociation,
    MemoryHit,
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
    from astrocytes.provider import DocumentStore, GraphStore, LLMProvider, VectorStore


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
        semantic_overfetch: int = 3,
    ) -> None:
        self.vector_store = vector_store
        self.llm_provider = llm_provider
        self.graph_store = graph_store
        self.document_store = document_store
        self.chunk_strategy = chunk_strategy
        self.max_chunk_size = max_chunk_size
        self.rrf_k = rrf_k
        self.semantic_overfetch = semantic_overfetch
        self._dedup = DedupDetector(similarity_threshold=0.95)

    async def retain(self, request: RetainRequest) -> RetainResult:
        """Retain pipeline: chunk → extract entities → embed → store."""
        # 1. Chunk
        chunks = chunk_text(request.content, strategy=self.chunk_strategy, max_chunk_size=self.max_chunk_size)
        if not chunks:
            return RetainResult(stored=False, error="No content after chunking")

        # 2. Generate embeddings for all chunks
        embeddings = await generate_embeddings(chunks, self.llm_provider)

        # 2b. Dedup check — skip if content is near-duplicate of existing memory
        is_dup, sim = self._dedup.is_duplicate(request.bank_id, embeddings[0])
        if is_dup:
            return RetainResult(stored=False, deduplicated=True, error=f"Near-duplicate (similarity={sim:.3f})")

        # 3. Extract entities (from full content, not per-chunk)
        entities = await extract_entities(request.content, self.llm_provider)

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
                    metadata=request.metadata,
                    tags=request.tags,
                    fact_type="world",  # Default; could be classified by LLM
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

        # 4. RRF fusion
        ranked_lists = [results for results in strategy_results.values() if results]
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

        return RecallResult(
            hits=hits,
            total_available=len(fused),
            truncated=truncated,
            trace=RecallTrace(
                strategies_used=list(strategy_results.keys()),
                total_candidates=sum(len(r) for r in strategy_results.values()),
                fusion_method="rrf",
            ),
        )

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        """Reflect pipeline: recall → LLM synthesis."""
        # 1. Run recall with larger result set
        recall_request = RecallRequest(
            query=request.query,
            bank_id=request.bank_id,
            max_results=20,  # Larger set for synthesis context
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
