"""In-memory provider implementations for testing.

These are fully functional providers backed by Python dicts/lists.
Used by conformance tests and integration tests.
"""

from __future__ import annotations

import math
import uuid
from typing import ClassVar

from astrocyte.types import (
    Completion,
    Document,
    DocumentFilters,
    DocumentHit,
    EngineCapabilities,
    Entity,
    EntityLink,
    ForgetRequest,
    ForgetResult,
    GraphHit,
    HealthStatus,
    LLMCapabilities,
    MemoryEntityAssociation,
    MemoryHit,
    Message,
    RecallRequest,
    RecallResult,
    ReflectRequest,
    ReflectResult,
    RetainRequest,
    RetainResult,
    TokenUsage,
    VectorFilters,
    VectorHit,
    VectorItem,
)


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# In-memory Vector Store
# ---------------------------------------------------------------------------


class InMemoryVectorStore:
    """Fully functional in-memory vector store for testing."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(self) -> None:
        self._vectors: dict[str, VectorItem] = {}

    async def store_vectors(self, items: list[VectorItem]) -> list[str]:
        ids = []
        for item in items:
            self._vectors[item.id] = item
            ids.append(item.id)
        return ids

    async def search_similar(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int = 10,
        filters: VectorFilters | None = None,
    ) -> list[VectorHit]:
        results: list[tuple[float, VectorItem]] = []
        for item in self._vectors.values():
            if item.bank_id != bank_id:
                continue
            if filters:
                if filters.tags and item.tags:
                    if not set(filters.tags) & set(item.tags):
                        continue
                if filters.fact_types and item.fact_type:
                    if item.fact_type not in filters.fact_types:
                        continue
            sim = _cosine_sim(query_vector, item.vector)
            results.append((sim, item))

        results.sort(key=lambda x: x[0], reverse=True)
        return [
            VectorHit(
                id=item.id,
                text=item.text,
                score=sim,
                metadata=item.metadata,
                tags=item.tags,
                fact_type=item.fact_type,
                occurred_at=item.occurred_at,
                memory_layer=item.memory_layer,
            )
            for sim, item in results[:limit]
        ]

    async def delete(self, ids: list[str], bank_id: str) -> int:
        count = 0
        for vid in ids:
            if vid in self._vectors and self._vectors[vid].bank_id == bank_id:
                del self._vectors[vid]
                count += 1
        return count

    async def list_vectors(
        self,
        bank_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> list[VectorItem]:
        bank_items = sorted(
            (v for v in self._vectors.values() if v.bank_id == bank_id),
            key=lambda v: v.id,
        )
        return bank_items[offset : offset + limit]

    async def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, message="in-memory vector store")


# ---------------------------------------------------------------------------
# In-memory Graph Store
# ---------------------------------------------------------------------------


class InMemoryGraphStore:
    """Fully functional in-memory graph store for testing."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        self._links: list[EntityLink] = []
        self._memory_entity_map: list[MemoryEntityAssociation] = []

    async def store_entities(self, entities: list[Entity], bank_id: str) -> list[str]:
        ids = []
        for entity in entities:
            self._entities[entity.id] = entity
            ids.append(entity.id)
        return ids

    async def store_links(self, links: list[EntityLink], bank_id: str) -> list[str]:
        ids = []
        for link in links:
            lid = uuid.uuid4().hex[:12]
            self._links.append(link)
            ids.append(lid)
        return ids

    async def link_memories_to_entities(
        self,
        associations: list[MemoryEntityAssociation],
        bank_id: str,
    ) -> None:
        self._memory_entity_map.extend(associations)

    async def query_neighbors(
        self,
        entity_ids: list[str],
        bank_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> list[GraphHit]:
        # Find memories linked to these entities
        memory_ids: set[str] = set()
        for assoc in self._memory_entity_map:
            if assoc.entity_id in entity_ids:
                memory_ids.add(assoc.memory_id)

        return [
            GraphHit(
                memory_id=mid,
                text=f"[graph result for {mid}]",
                connected_entities=list(entity_ids),
                depth=1,
                score=0.5,
            )
            for mid in list(memory_ids)[:limit]
        ]

    async def query_entities(self, query: str, bank_id: str, limit: int = 10) -> list[Entity]:
        query_lower = query.lower()
        results = [e for e in self._entities.values() if query_lower in e.name.lower()]
        return results[:limit]

    async def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, message="in-memory graph store")


# ---------------------------------------------------------------------------
# In-memory Document Store
# ---------------------------------------------------------------------------


class InMemoryDocumentStore:
    """Fully functional in-memory document store for testing."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(self) -> None:
        self._docs: dict[str, tuple[Document, str]] = {}  # id -> (doc, bank_id)

    async def store_document(self, document: Document, bank_id: str) -> str:
        self._docs[document.id] = (document, bank_id)
        return document.id

    async def search_fulltext(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
        filters: DocumentFilters | None = None,
    ) -> list[DocumentHit]:
        query_terms = set(query.lower().split())
        results: list[tuple[float, Document]] = []
        for doc, bid in self._docs.values():
            if bid != bank_id:
                continue
            doc_terms = set(doc.text.lower().split())
            overlap = len(query_terms & doc_terms)
            if overlap > 0:
                score = overlap / max(len(query_terms), 1)
                results.append((score, doc))

        results.sort(key=lambda x: x[0], reverse=True)
        return [
            DocumentHit(document_id=doc.id, text=doc.text, score=score, metadata=doc.metadata)
            for score, doc in results[:limit]
        ]

    async def get_document(self, document_id: str, bank_id: str) -> Document | None:
        entry = self._docs.get(document_id)
        if entry and entry[1] == bank_id:
            return entry[0]
        return None

    async def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, message="in-memory document store")


# ---------------------------------------------------------------------------
# In-memory Engine Provider
# ---------------------------------------------------------------------------


class InMemoryEngineProvider:
    """Fully functional in-memory engine provider for testing."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(self, supports_reflect: bool = True, supports_forget: bool = True) -> None:
        self._memories: dict[str, list[MemoryHit]] = {}  # bank_id -> memories
        self._supports_reflect = supports_reflect
        self._supports_forget = supports_forget

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            supports_reflect=self._supports_reflect,
            supports_forget=self._supports_forget,
            supports_semantic_search=True,
            supports_tags=True,
            supports_metadata=True,
        )

    async def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, message="in-memory engine")

    async def retain(self, request: RetainRequest) -> RetainResult:
        if request.bank_id not in self._memories:
            self._memories[request.bank_id] = []
        mem_id = uuid.uuid4().hex[:16]
        self._memories[request.bank_id].append(
            MemoryHit(
                text=request.content,
                score=1.0,
                fact_type="world",
                metadata=request.metadata,
                tags=request.tags,
                occurred_at=request.occurred_at,
                source=request.source,
                memory_id=mem_id,
                bank_id=request.bank_id,
            )
        )
        return RetainResult(stored=True, memory_id=mem_id)

    async def recall(self, request: RecallRequest) -> RecallResult:
        memories = self._memories.get(request.bank_id, [])

        # Apply filters before scoring
        filtered = memories
        if request.tags:
            tag_set = set(request.tags)
            filtered = [m for m in filtered if m.tags and tag_set & set(m.tags)]
        if request.fact_types:
            ft_set = set(request.fact_types)
            filtered = [m for m in filtered if m.fact_type in ft_set]
        if request.time_range:
            t_start, t_end = request.time_range
            filtered = [m for m in filtered if m.occurred_at and t_start <= m.occurred_at <= t_end]

        # Wildcard query returns all memories (used by export)
        if request.query.strip() == "*":
            scored = [(1.0, mem) for mem in filtered]
        else:
            # Simple keyword matching
            query_terms = set(request.query.lower().split())
            scored = []
            for mem in filtered:
                mem_terms = set(mem.text.lower().split())
                overlap = len(query_terms & mem_terms)
                if overlap > 0:
                    score = overlap / max(len(query_terms), 1)
                    scored.append((score, mem))

        scored.sort(key=lambda x: x[0], reverse=True)
        hits = [
            MemoryHit(
                text=mem.text,
                score=score,
                fact_type=mem.fact_type,
                metadata=mem.metadata,
                tags=mem.tags,
                occurred_at=mem.occurred_at,
                source=mem.source,
                memory_id=mem.memory_id,
                bank_id=request.bank_id,
            )
            for score, mem in scored[: request.max_results]
        ]
        return RecallResult(hits=hits, total_available=len(scored), truncated=len(scored) > request.max_results)

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        if not self._supports_reflect:
            raise NotImplementedError("reflect not supported")
        recall_result = await self.recall(RecallRequest(query=request.query, bank_id=request.bank_id))
        answer = "Synthesis: " + "; ".join(h.text for h in recall_result.hits[:5])
        return ReflectResult(answer=answer, sources=recall_result.hits[:5])

    async def forget(self, request: ForgetRequest) -> ForgetResult:
        if not self._supports_forget:
            raise NotImplementedError("forget not supported")
        bank_memories = self._memories.get(request.bank_id, [])

        if request.scope == "all":
            count = len(bank_memories)
            self._memories[request.bank_id] = []
            return ForgetResult(deleted_count=count)

        if request.memory_ids:
            ids_set = set(request.memory_ids)
            before = len(bank_memories)
            bank_memories = [m for m in bank_memories if m.memory_id not in ids_set]
            self._memories[request.bank_id] = bank_memories
            return ForgetResult(deleted_count=before - len(bank_memories))

        # Tag-based and/or date-based deletion
        if request.tags or request.before_date:
            before = len(bank_memories)
            tag_set = set(request.tags) if request.tags else None

            def _should_keep(m: MemoryHit) -> bool:
                if tag_set and m.tags and tag_set & set(m.tags):
                    return False
                if request.before_date and m.occurred_at and m.occurred_at < request.before_date:
                    return False
                return True

            bank_memories = [m for m in bank_memories if _should_keep(m)]
            self._memories[request.bank_id] = bank_memories
            return ForgetResult(deleted_count=before - len(bank_memories))

        return ForgetResult(deleted_count=0)


# ---------------------------------------------------------------------------
# Mock LLM Provider
# ---------------------------------------------------------------------------


class MockLLMProvider:
    """Mock LLM provider that returns deterministic responses for testing."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(self, default_response: str = "Mock LLM response") -> None:
        self._default_response = default_response
        self._call_count = 0

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities()

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        self._call_count += 1
        # Check if entity extraction prompt (may be in system or user message)
        all_text = " ".join(m.content for m in messages if isinstance(m.content, str))
        if "extract named entities" in all_text.lower():
            return Completion(
                text='[{"name": "Test Entity", "entity_type": "OTHER", "aliases": []}]',
                model=model or "mock",
                usage=TokenUsage(input_tokens=10, output_tokens=20),
            )
        return Completion(
            text=self._default_response,
            model=model or "mock",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Generate deterministic pseudo-embeddings for testing."""
        import hashlib

        result = []
        for text in texts:
            h = hashlib.sha256(text.encode()).digest()
            raw = [float(b) / 255.0 for b in h]
            while len(raw) < 128:
                h = hashlib.sha256(h).digest()
                raw.extend(float(b) / 255.0 for b in h)
            raw = raw[:128]
            norm = math.sqrt(sum(x * x for x in raw))
            if norm > 0:
                raw = [x / norm for x in raw]
            result.append(raw)
        return result
