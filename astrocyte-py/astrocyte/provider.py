"""Astrocyte provider protocols — the SPIs that backends implement.

These are abstract contracts (Python Protocols), not concrete implementations.
Providers must implement every method in the relevant Protocol to be recognized
by Astrocyte's runtime type check (``isinstance(obj, VectorStore)`` etc.).

Built-in implementations:
- In-memory test doubles: ``astrocyte.testing.in_memory``
- PostgreSQL + pgvector: ``astrocyte_postgres.store.PostgresStore``
- Optional wheels (repo ``adapters-storage-py/``): ``astrocyte-postgres``, ``astrocyte-qdrant``, ``astrocyte-neo4j``, ``astrocyte-elasticsearch``

Each protocol has a SPI_VERSION ClassVar for compatibility checking.
All methods are async except capabilities() and transport methods.

SPI versioning: Astrocyte checks SPI_VERSION at registration time.
- Version 1: base protocol (all methods required)
- Version 2+: may add optional methods; Astrocyte adapts calls accordingly.
Providers with unrecognized versions are rejected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from astrocyte.types import (
        Completion,
        Document,
        DocumentFilters,
        DocumentHit,
        EngineCapabilities,
        Entity,
        EntityCandidateMatch,
        EntityLink,
        ForgetRequest,
        ForgetResult,
        GraphHit,
        HealthStatus,
        HttpClientContext,
        LLMCapabilities,
        MemoryEntityAssociation,
        MemoryLink,
        MentalModel,
        Message,
        RecallRequest,
        RecallResult,
        ReflectRequest,
        ReflectResult,
        RetainRequest,
        RetainResult,
        ToolDefinition,
        TransportCapabilities,
        VectorFilters,
        VectorHit,
        VectorItem,
        WikiPage,
    )


# ---------------------------------------------------------------------------
# Tier 1: Storage Providers
# ---------------------------------------------------------------------------


@runtime_checkable
class VectorStore(Protocol):
    """SPI for vector database adapters. Required for Tier 1.

    Implement this protocol to connect Astrocyte to a vector database
    (e.g., pgvector, Qdrant, Pinecone). Astrocyte uses ``@runtime_checkable``
    to verify your class satisfies this contract at registration time —
    no inheritance required, just implement every method below.

    See ``astrocyte_postgres.store.PostgresStore`` for a production example,
    or ``astrocyte.testing.in_memory.InMemoryVectorStore`` for a minimal reference.
    """

    SPI_VERSION: ClassVar[int] = 1

    async def store_vectors(self, items: list[VectorItem]) -> list[str]:
        """Persist vectors with metadata. Upsert semantics — existing IDs are overwritten.

        Implementations must validate that each item's vector length matches the
        configured embedding dimensions.

        Returns:
            List of stored IDs (same order as ``items``). If ``items`` is empty,
            implementations must treat this as a no-op and return an empty list.

        Raises:
            ValueError: If any vector's length does not match embedding dimensions.
        """
        pass

    async def search_similar(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int = 10,
        filters: VectorFilters | None = None,
    ) -> list[VectorHit]:
        """Find vectors most similar to ``query_vector`` within a bank.

        Returns:
            Hits sorted by similarity descending. Each hit's ``score`` must be
            in the range [0.0, 1.0] where 1.0 is an exact match. Returns an
            empty list if ``bank_id`` does not exist.

        Raises:
            ValueError: If ``query_vector`` length does not match embedding dimensions.
        """
        pass

    async def delete(self, ids: list[str], bank_id: str) -> int:
        """Delete vectors by ID within a bank. Bank isolation is required —
        an ID in ``bank-2`` must not be deleted when ``bank_id`` is ``bank-1``.

        Returns:
            Count of vectors actually deleted. Returns 0 for empty ``ids`` list.
        """
        pass

    async def list_vectors(
        self,
        bank_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> list[VectorItem]:
        """List vectors in a bank with pagination. Used by consolidation
        to scan and deduplicate vectors.

        Implementations must return vectors in a **stable order** (e.g., by ID)
        so that pagination produces consistent, complete results.

        Returns:
            Up to ``limit`` vectors starting at ``offset``. Empty list if
            the bank does not exist or ``offset`` is past the end.
        """
        pass

    async def health(self) -> HealthStatus:
        """Check database connectivity.

        Returns:
            ``HealthStatus(healthy=True, ...)`` if the database is reachable,
            ``HealthStatus(healthy=False, ...)`` with a diagnostic message otherwise.
        """
        pass


@runtime_checkable
class GraphStore(Protocol):
    """SPI for graph database adapters. Optional for Tier 1.

    Implement this protocol to add entity/relationship-based retrieval
    alongside vector search. Astrocyte's multi-strategy retrieval pipeline
    will query both VectorStore and GraphStore in parallel when available.
    """

    SPI_VERSION: ClassVar[int] = 1

    async def store_entities(self, entities: list[Entity], bank_id: str) -> list[str]:
        """Persist or update entities (people, concepts, objects) in the graph.

        Returns:
            List of stored entity IDs.
        """
        pass

    async def store_links(self, links: list[EntityLink], bank_id: str) -> list[str]:
        """Persist relationships between entities.

        Returns:
            List of stored link IDs.
        """
        pass

    async def link_memories_to_entities(
        self,
        associations: list[MemoryEntityAssociation],
        bank_id: str,
    ) -> None:
        """Associate memory IDs (from VectorStore) with entity IDs in the graph.

        This enables graph-based recall: "find memories connected to entity X."
        """
        pass

    async def store_memory_links(
        self,
        links: list["MemoryLink"],
        bank_id: str,
    ) -> list[str]:
        """Persist directional memory-to-memory links (Hindsight parity).

        Used for causal chains (``caused_by``) and the precomputed
        semantic kNN graph (``semantic``). Adapters that don't
        implement this should silently no-op (return ``[]``) — the
        spread / link-expansion path probes via ``getattr`` and
        degrades to entity-only when the method is absent.

        Returns the list of stored link IDs.
        """
        return []

    async def find_memory_links(
        self,
        seed_memory_ids: list[str],
        bank_id: str,
        *,
        link_types: list[str] | None = None,
        limit: int = 200,
    ) -> list["MemoryLink"]:
        """Return memory links touching any seed (either direction).

        Adapter contract: results SHOULD include links where the seed
        is the source OR the target — the link-expansion retrieval
        treats both directions for ``semantic`` and only the target
        direction for ``caused_by`` (effect → cause). Callers filter
        as needed.

        Adapters that haven't implemented this return ``[]``; the
        link-expansion retrieval probes via ``getattr`` and skips the
        memory-link signal when the method is absent.
        """
        return []

    async def query_neighbors(
        self,
        entity_ids: list[str],
        bank_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> list[GraphHit]:
        """Traverse the graph from ``entity_ids`` up to ``max_depth`` hops
        and return connected memories.

        Returns:
            Graph hits sorted by relevance (e.g., shortest path, edge weight).
        """
        pass

    async def query_entities(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
    ) -> list[Entity]:
        """Search entities by name or alias (fuzzy or exact match).

        Returns:
            Matching entities, useful for resolving entity references before
            calling ``query_neighbors``.
        """
        pass

    async def find_entity_candidates(
        self,
        name: str,
        bank_id: str,
        threshold: float = 0.8,
        limit: int = 5,
    ) -> list[Entity]:
        """Return entities whose name is similar to *name* above *threshold*.

        Used by the entity resolution pipeline (M11) to find candidate
        entities that may be aliases of a newly-extracted entity before
        calling the LLM confirmation step.

        A simple implementation may use case-insensitive substring matching;
        production adapters should use vector similarity or full-text search.

        Returns:
            Candidate entities ordered by similarity descending.
        """
        pass

    async def find_entity_candidates_scored(
        self,
        name: str,
        bank_id: str,
        *,
        name_embedding: list[float] | None = None,
        trigram_threshold: float = 0.3,
        limit: int = 10,
    ) -> list[EntityCandidateMatch]:
        """Return scored entity candidates for the Hindsight entity-resolution cascade.

        Scores each candidate against two cheap signals:

        - ``name_similarity`` — trigram similarity between the candidate's
          name and ``name`` in ``[0.0, 1.0]``. Production adapters use
          ``pg_trgm.similarity()``; the in-memory adapter approximates with
          :class:`difflib.SequenceMatcher`.
        - ``embedding_similarity`` — cosine similarity between the
          candidate's stored embedding and ``name_embedding``, or ``None``
          when either side has no embedding.

        The resolver uses these scores to autolink, skip, or escalate to LLM
        without further round-trips.

        Args:
            name: Surface form to resolve.
            bank_id: Bank scope.
            name_embedding: Optional embedding of ``name`` for the embedding
                tier. When ``None``, ``embedding_similarity`` is ``None`` for
                every result and the cascade falls back to trigram + LLM.
            trigram_threshold: Adapter-side prefilter — candidates with a
                trigram similarity strictly below this value are dropped.
                Default ``0.3`` matches PostgreSQL's ``pg_trgm.similarity_threshold``.
            limit: Maximum candidates returned, ordered by descending score.

        Returns:
            ``list[EntityCandidateMatch]`` ordered by best score first.
            Defaults to an empty list when no candidates clear the threshold.
        """
        pass

    async def store_entity_link(self, link: EntityLink, bank_id: str) -> str:
        """Persist a single typed relationship between two entities.

        Unlike ``store_links`` (bulk, co-occurrence), this method is called
        by the entity resolution pipeline for confirmed alias links that carry
        ``evidence`` and ``confidence``.

        Returns:
            The stored link ID.
        """
        pass

    async def health(self) -> HealthStatus:
        """Check database connectivity."""
        pass


@runtime_checkable
class DocumentStore(Protocol):
    """SPI for document storage with full-text search. Optional for Tier 1.

    Implement this protocol to add BM25/lexical search alongside vector
    similarity. Useful for keyword-heavy queries where semantic search
    alone may miss exact matches.
    """

    SPI_VERSION: ClassVar[int] = 1

    async def store_document(self, document: Document, bank_id: str) -> str:
        """Persist a source document for full-text indexing.

        Returns:
            The stored document ID.
        """
        pass

    async def search_fulltext(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
        filters: DocumentFilters | None = None,
    ) -> list[DocumentHit]:
        """BM25 full-text search over stored content.

        Returns:
            Hits sorted by BM25 relevance score descending.
        """
        pass

    async def get_document(self, document_id: str, bank_id: str) -> Document | None:
        """Retrieve a single document by ID.

        Returns:
            The document, or ``None`` if not found in this bank.
        """
        pass

    async def health(self) -> HealthStatus:
        """Check database connectivity."""
        pass


# ---------------------------------------------------------------------------
# Tier 1: Wiki Store (M8)
# ---------------------------------------------------------------------------


@runtime_checkable
class WikiStore(Protocol):
    """SPI for wiki page storage (M8 LLM wiki compile). Optional.

    WikiStore persists structured WikiPage metadata. Vector embeddings of
    compiled pages are stored separately in the VectorStore with
    ``memory_layer="compiled"`` and ``fact_type="wiki"``, so recall tiering
    can search them via the standard ``search_similar`` path.

    Implement this protocol to enable ``brain.compile()`` persistence.
    See ``astrocyte.testing.in_memory.InMemoryWikiStore`` for a reference.
    """

    SPI_VERSION: ClassVar[int] = 1

    async def upsert_page(self, page: WikiPage, bank_id: str) -> str:
        """Create or update a wiki page. Upsert semantics — if a page with
        the same ``page_id`` exists in this bank, its revision is incremented
        and content replaced. The previous revision is archived (not deleted).

        Returns:
            The stored ``page_id``.
        """
        pass

    async def get_page(self, page_id: str, bank_id: str) -> WikiPage | None:
        """Retrieve the current revision of a wiki page by ID.

        Returns:
            The page, or ``None`` if not found in this bank.
        """
        pass

    async def list_pages(
        self,
        bank_id: str,
        scope: str | None = None,
        kind: str | None = None,
    ) -> list[WikiPage]:
        """List current-revision wiki pages for a bank.

        Args:
            bank_id: The bank to list pages for.
            scope: If set, return only pages whose ``scope`` matches exactly.
            kind: If set, return only pages of this kind ("entity", "topic", "concept").

        Returns:
            All matching pages, unsorted.
        """
        pass

    async def delete_page(self, page_id: str, bank_id: str) -> bool:
        """Delete a wiki page (current revision and audit log).

        Returns:
            ``True`` if the page was found and deleted, ``False`` if not found.
        """
        pass

    async def health(self) -> HealthStatus:
        """Check storage connectivity."""
        pass


# ---------------------------------------------------------------------------
# Tier 1: Mental Model Store (first-class — see Hindsight comparison)
# ---------------------------------------------------------------------------


@runtime_checkable
class MentalModelStore(Protocol):
    """SPI for first-class mental-model storage.

    Mental models are curated, refreshable saved-reflect summaries — the
    "Caroline prefers async updates" / "Project X status: blocked on
    review" durable artifacts that live longer than any single recall.

    Previously these piggybacked on :class:`WikiStore` via
    ``kind="concept"`` + ``metadata["_mental_model"] = True``, which
    overloaded the wiki layer's lifecycle (revisions, lint issues,
    cross_links) for a fundamentally different concept. This SPI breaks
    them out into their own table with their own lifecycle and version
    history.

    See ``astrocyte.testing.in_memory.InMemoryMentalModelStore`` for a
    reference and ``astrocyte_postgres.PostgresMentalModelStore`` for the
    production-grade implementation.
    """

    SPI_VERSION: ClassVar[int] = 1

    async def upsert(self, model: "MentalModel", bank_id: str) -> int:
        """Create or refresh a mental model. Upsert semantics — if a
        model with the same ``model_id`` exists in this bank, the
        revision is incremented and the new content replaces the
        current; the previous revision is archived (not deleted).

        Args:
            model: The mental model to store. ``revision`` and
                ``refreshed_at`` are assigned by the store.
            bank_id: Tenant-scoped bank identifier.

        Returns:
            The new revision number.
        """
        pass

    async def get(self, model_id: str, bank_id: str) -> "MentalModel | None":
        """Retrieve the current revision of a mental model.

        Returns ``None`` if the model doesn't exist or has been deleted.
        """
        pass

    async def list(
        self,
        bank_id: str,
        *,
        scope: str | None = None,
    ) -> list["MentalModel"]:
        """List current-revision mental models in a bank.

        Args:
            bank_id: Tenant-scoped bank identifier.
            scope: If set, return only models with matching scope
                (e.g. ``"person:alice"``, or the default ``"bank"``).
        """
        pass

    async def delete(self, model_id: str, bank_id: str) -> bool:
        """Soft-delete a mental model.

        Returns ``True`` if the model was found and deleted, ``False`` if
        it didn't exist or was already deleted. Implementations may keep
        the row for audit (deleted_at timestamp) or hard-delete; either
        is acceptable as long as ``get`` returns ``None`` afterward.
        """
        pass

    async def health(self) -> HealthStatus:
        """Check storage connectivity."""
        pass


# ---------------------------------------------------------------------------
# Tier 2: Engine Provider
# ---------------------------------------------------------------------------


@runtime_checkable
class EngineProvider(Protocol):
    """SPI for full-stack memory engines (Tier 2).

    Tier 2 engines own the entire memory pipeline — embedding, storage,
    retrieval, and synthesis. Astrocyte delegates retain/recall/reflect
    to the engine but still enforces its policy layer (PII, rate limits,
    access control) around every call.

    Examples of Tier 2 engines: Mem0, Zep, Mystique/Hindsight.
    """

    SPI_VERSION: ClassVar[int] = 1

    def capabilities(self) -> EngineCapabilities:
        """Declare what this engine supports (reflect, forget, etc.).

        Called once at init. Astrocyte uses this to decide which operations
        to delegate vs. handle internally.
        """
        pass

    async def health(self) -> HealthStatus:
        """Liveness and readiness check."""
        pass

    async def retain(self, request: RetainRequest) -> RetainResult:
        """Store content into memory. The engine decides how to chunk,
        embed, and index the content.
        """
        pass

    async def recall(self, request: RecallRequest) -> RecallResult:
        """Retrieve relevant memories for a query. The engine owns
        the retrieval strategy (semantic, keyword, graph, hybrid).
        """
        pass

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        """Synthesize an answer from memory. Optional — only called if
        ``capabilities().supports_reflect`` is True.
        """
        pass

    async def forget(self, request: ForgetRequest) -> ForgetResult:
        """Remove or archive memories. Optional — only called if
        ``capabilities().supports_forget`` is True.
        """
        pass


# ---------------------------------------------------------------------------
# LLM Provider
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """SPI for LLM access needed by the Astrocyte core pipeline.

    Tier 1 deployments require an LLMProvider for embedding generation
    and (optionally) for reflect-time synthesis. Tier 2 engines typically
    bring their own LLM access, but Astrocyte may still use this for
    MIP intent routing or policy evaluation.
    """

    SPI_VERSION: ClassVar[int] = 1

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = None,
    ) -> Completion:
        """Generate a text completion from a message sequence.

        Used by reflect, MIP intent routing, and consolidation summarization.

        ``tools`` (optional) — when provided, the provider passes them as
        native function-calling tools to the underlying API and the
        returned :class:`Completion`'s ``tool_calls`` field carries any
        invocations the model emitted. ``tool_choice`` may be ``"auto"``
        (default when tools is set), ``"required"`` (force a tool call),
        or a specific tool name. Providers that don't support tools
        SHOULD ignore both args and return ``tool_calls=None`` so callers
        can feature-detect.
        """
        pass

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """Generate embedding vectors for the given texts.

        Used by the Tier 1 retain pipeline to embed content before storage.

        Raises:
            NotImplementedError: If this provider does not support embeddings
                (e.g., a local model without an embedding endpoint).
        """
        pass

    def capabilities(self) -> LLMCapabilities:
        """Declare LLM capabilities (supported models, embedding dimensions, etc.)."""
        pass


# ---------------------------------------------------------------------------
# Outbound Transport Provider (optional, cross-cutting)
# ---------------------------------------------------------------------------


@runtime_checkable
class OutboundTransportProvider(Protocol):
    """SPI for credential gateways and enterprise HTTP/TLS proxies.

    Optional, cross-cutting provider. When registered, Astrocyte calls
    ``apply()`` on every outbound HTTP client context (e.g., to inject
    auth headers, configure mTLS, or route through a corporate proxy).
    """

    SPI_VERSION: ClassVar[int] = 1

    def apply(self, ctx: HttpClientContext) -> None:
        """Configure the HTTP client context before an outbound request.

        Typical uses: inject Bearer tokens, set proxy URLs, configure TLS
        client certificates.
        """
        pass

    def subprocess_env(self) -> dict[str, str]:
        """Return environment variables for subprocess calls that need
        the same transport configuration (e.g., ``HTTPS_PROXY``, auth tokens).
        """
        pass

    def capabilities(self) -> TransportCapabilities:
        """Declare transport capabilities (proxy support, mTLS, etc.)."""
        pass


# ---------------------------------------------------------------------------
# SPI Version Negotiation
# ---------------------------------------------------------------------------

# Supported SPI versions per protocol
_SUPPORTED_VERSIONS: dict[str, set[int]] = {
    "VectorStore": {1},
    "GraphStore": {1},
    "DocumentStore": {1},
    "WikiStore": {1},
    "MentalModelStore": {1},
    "EngineProvider": {1},
    "LLMProvider": {1},
    "OutboundTransportProvider": {1},
}


def check_spi_version(provider: object, protocol_name: str) -> int:
    """Validate a provider's SPI_VERSION against supported versions.

    Returns the provider's version if accepted.
    Raises ConfigError if the version is unsupported.
    """
    from astrocyte.errors import ConfigError

    version = getattr(provider, "SPI_VERSION", None)
    if version is None:
        # No version declared — assume v1 for backwards compatibility
        return 1

    supported = _SUPPORTED_VERSIONS.get(protocol_name, {1})
    if version not in supported:
        raise ConfigError(
            f"{protocol_name} SPI version {version} is not supported. Supported versions: {sorted(supported)}"
        )
    return version
