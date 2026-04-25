"""In-memory provider implementations for testing.

These are fully functional providers backed by Python dicts/lists.
Used by conformance tests and integration tests.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
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
    WikiPage,
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
                # M9: time-travel filter — exclude items retained after as_of
                if filters.as_of is not None and item.retained_at is not None:
                    if item.retained_at > filters.as_of:
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
                retained_at=item.retained_at,  # M9
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
        self._entities: dict[str, dict[str, Entity]] = {}  # bank_id → {entity_id → Entity}
        self._links: dict[str, list[EntityLink]] = {}  # bank_id → links
        self._memory_entity_map: dict[str, list[MemoryEntityAssociation]] = {}  # bank_id → assocs

    async def store_entities(self, entities: list[Entity], bank_id: str) -> list[str]:
        if bank_id not in self._entities:
            self._entities[bank_id] = {}
        ids = []
        for entity in entities:
            self._entities[bank_id][entity.id] = entity
            ids.append(entity.id)
        return ids

    async def store_links(self, links: list[EntityLink], bank_id: str) -> list[str]:
        if bank_id not in self._links:
            self._links[bank_id] = []
        ids = []
        for link in links:
            lid = uuid.uuid4().hex[:12]
            self._links[bank_id].append(link)
            ids.append(lid)
        return ids

    async def link_memories_to_entities(
        self,
        associations: list[MemoryEntityAssociation],
        bank_id: str,
    ) -> None:
        if bank_id not in self._memory_entity_map:
            self._memory_entity_map[bank_id] = []
        self._memory_entity_map[bank_id].extend(associations)

    async def query_neighbors(
        self,
        entity_ids: list[str],
        bank_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> list[GraphHit]:
        # Find memories linked to these entities within this bank
        memory_ids: set[str] = set()
        for assoc in self._memory_entity_map.get(bank_id, []):
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
        bank_entities = self._entities.get(bank_id, {})
        results = [e for e in bank_entities.values() if query_lower in e.name.lower()]
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
        # Stamp _created_at so MIP forget min_age_days can be enforced. Existing
        # callers that already set _created_at (eg. import/replay) win.
        meta = dict(request.metadata) if request.metadata else {}
        meta.setdefault("_created_at", datetime.now(UTC).isoformat())
        self._memories[request.bank_id].append(
            MemoryHit(
                text=request.content,
                score=1.0,
                fact_type="world",
                metadata=meta,
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
        # MIP soft-delete: hide records flagged with `_deleted: true` in metadata.
        # The flag is set by `soft_delete()` when forget.mode == "soft" so that
        # records survive on disk for audit/restore but disappear from recall.
        filtered = [
            m for m in memories
            if not (m.metadata and m.metadata.get("_deleted") is True)
        ]
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

    async def soft_delete(self, bank_id: str, memory_ids: list[str]) -> int:
        """Mark records as soft-deleted by setting ``_deleted: true`` in metadata.

        Used by MIP forget when ``forget.mode == "soft"``: records remain in
        the store (auditable, restorable) but are hidden from recall.
        Returns the number of records that were transitioned to deleted.
        """
        if not memory_ids:
            return 0
        ids_set = set(memory_ids)
        count = 0
        for m in self._memories.get(bank_id, []):
            if m.memory_id in ids_set:
                meta = dict(m.metadata) if m.metadata else {}
                if meta.get("_deleted") is True:
                    continue  # already soft-deleted
                meta["_deleted"] = True
                m.metadata = meta
                count += 1
        return count

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
# In-memory Wiki Store (M8)
# ---------------------------------------------------------------------------


class InMemoryWikiStore:
    """Fully functional in-memory wiki store for testing (M8).

    Stores current-revision pages and an audit log of past revisions.
    Vector embeddings of pages are managed separately by the VectorStore
    (stored with ``memory_layer="compiled"``); this store holds only the
    structured WikiPage metadata.
    """

    SPI_VERSION: ClassVar[int] = 1

    def __init__(self) -> None:
        # bank_id → {page_id → WikiPage} (current revisions)
        self._pages: dict[str, dict[str, WikiPage]] = {}
        # bank_id → {page_id → list[WikiPage]} (past revisions, newest last)
        self._history: dict[str, dict[str, list[WikiPage]]] = {}

    async def upsert_page(self, page: WikiPage, bank_id: str) -> str:
        if bank_id not in self._pages:
            self._pages[bank_id] = {}
            self._history[bank_id] = {}

        existing = self._pages[bank_id].get(page.page_id)
        if existing is not None:
            # Archive current revision before replacing
            self._history[bank_id].setdefault(page.page_id, []).append(existing)
            # Increment revision on the incoming page
            from dataclasses import replace as _replace
            page = _replace(page, revision=existing.revision + 1)

        self._pages[bank_id][page.page_id] = page
        return page.page_id

    async def get_page(self, page_id: str, bank_id: str) -> WikiPage | None:
        return self._pages.get(bank_id, {}).get(page_id)

    async def list_pages(
        self,
        bank_id: str,
        scope: str | None = None,
        kind: str | None = None,
    ) -> list[WikiPage]:
        pages = list(self._pages.get(bank_id, {}).values())
        if scope is not None:
            pages = [p for p in pages if p.scope == scope]
        if kind is not None:
            pages = [p for p in pages if p.kind == kind]
        return pages

    async def delete_page(self, page_id: str, bank_id: str) -> bool:
        bank_pages = self._pages.get(bank_id, {})
        if page_id not in bank_pages:
            return False
        del bank_pages[page_id]
        self._history.get(bank_id, {}).pop(page_id, None)
        return True

    async def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, message="in-memory wiki store")

    def revision_history(self, page_id: str, bank_id: str) -> list[WikiPage]:
        """Return past revisions for a page (oldest first). Testing helper."""
        return list(self._history.get(bank_id, {}).get(page_id, []))


# ---------------------------------------------------------------------------
# Mock LLM Provider
# ---------------------------------------------------------------------------


class MockLLMProvider:
    """Mock LLM provider with bag-of-words embeddings and extractive synthesis.

    Embeddings use term-frequency vectors over a shared vocabulary, so
    semantically related texts have high cosine similarity — enabling
    meaningful vector retrieval without an API key.

    Synthesis extracts the most relevant memory text from the prompt context
    rather than returning a static string.
    """

    SPI_VERSION: ClassVar[int] = 1

    _embed_dim: ClassVar[int] = 128

    def __init__(self, default_response: str = "Mock LLM response") -> None:
        self._default_response = default_response
        self._call_count = 0
        #: Last ``complete()`` user message content (for tests asserting prompt structure).
        self.last_user_message: str | None = None

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
        for m in reversed(messages):
            if m.role == "user" and isinstance(m.content, str):
                self.last_user_message = m.content
                break
        all_text = " ".join(m.content for m in messages if isinstance(m.content, str))
        # Entity extraction prompt
        if "extract named entities" in all_text.lower():
            return Completion(
                text='[{"name": "Test Entity", "entity_type": "OTHER", "aliases": []}]',
                model=model or "mock",
                usage=TokenUsage(input_tokens=10, output_tokens=20),
            )
        # Memory synthesis prompt — extract most relevant memory text
        if "<memories>" in all_text and "<query>" in all_text:
            answer = _extractive_synthesize(all_text)
            return Completion(
                text=answer,
                model=model or "mock",
                usage=TokenUsage(input_tokens=50, output_tokens=30),
            )
        return Completion(
            text=self._default_response,
            model=model or "mock",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Generate bag-of-words embeddings with real semantic signal.

        Uses term-frequency vectors over a shared vocabulary. Texts that share
        words have high cosine similarity, enabling meaningful retrieval.
        """
        return [self._bow_embed(text) for text in texts]

    def _bow_embed(self, text: str) -> list[float]:
        """Build a term-frequency vector using the hashing trick.

        Each word maps to a bucket via ``hash(word) % dim`` and increments
        that dimension. All components are non-negative, guaranteeing
        non-negative cosine similarities between any two vectors — required
        because ``VectorHit.score`` enforces ``>= 0.0``.
        """
        import hashlib
        from string import punctuation

        dim = self._embed_dim
        vec = [0.0] * dim
        tokens = [t.strip(punctuation).lower() for t in text.split() if t.strip(punctuation)]

        for token in tokens:
            if not token:
                continue
            h = int(hashlib.md5(token.encode()).hexdigest(), 16)
            bucket = h % dim
            vec[bucket] += 1.0

        # L2 normalize
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec


def _normalize_terms(text: str) -> set[str]:
    """Lowercase, strip punctuation, drop short words."""
    from string import punctuation

    return {t.strip(punctuation).lower() for t in text.split() if len(t.strip(punctuation)) > 2}


def _extractive_synthesize(prompt_text: str) -> str:
    """Extract the most query-relevant memory from the synthesis prompt.

    Parses ``<memories>`` and ``<query>`` blocks from the reflect prompt,
    scores each memory by word overlap with the query, and returns
    the top memories concatenated. This gives the mock provider
    meaningful reflect answers without a real LLM.
    """
    import re

    # Extract query
    query_match = re.search(r"<query>\s*(.*?)\s*</query>", prompt_text, re.DOTALL)
    if not query_match:
        return "No query found."
    query = query_match.group(1).strip()
    query_terms = _normalize_terms(query)

    # Extract individual memories
    memories_match = re.search(r"<memories>\s*(.*?)\s*</memories>", prompt_text, re.DOTALL)
    if not memories_match:
        return "No memories found."

    # Group continuation lines with their [Memory N] header.
    # Lines starting with [Memory N] start a new block; other lines
    # are appended to the current block.
    raw_lines = memories_match.group(1).strip().split("\n")
    blocks: list[str] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        if re.match(r"^\[Memory \d+\]", line):
            # Strip [Memory N] prefix for cleaner output
            text = re.sub(r"^\[Memory \d+\](\s*\([^)]*\))?(\s*\[[^\]]*\])?\s*:\s*", "", line)
            blocks.append(text)
        elif blocks:
            blocks[-1] += " " + line
        else:
            blocks.append(line)

    # Score each memory block by query term overlap
    scored: list[tuple[float, str]] = []
    for text in blocks:
        block_terms = _normalize_terms(text)
        overlap = len(query_terms & block_terms)
        if overlap > 0:
            scored.append((overlap, text))

    if not scored:
        return "I don't have relevant information to answer this question."

    scored.sort(key=lambda x: x[0], reverse=True)
    # Return top 3 most relevant memories
    top = [text for _, text in scored[:3]]
    return " ".join(top)
