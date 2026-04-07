"""Astrocyte provider protocols — the SPIs that backends implement.

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
        EntityLink,
        ForgetRequest,
        ForgetResult,
        GraphHit,
        HealthStatus,
        HttpClientContext,
        LLMCapabilities,
        MemoryEntityAssociation,
        Message,
        RecallRequest,
        RecallResult,
        ReflectRequest,
        ReflectResult,
        RetainRequest,
        RetainResult,
        TransportCapabilities,
        VectorFilters,
        VectorHit,
        VectorItem,
    )


# ---------------------------------------------------------------------------
# Tier 1: Storage Providers
# ---------------------------------------------------------------------------


@runtime_checkable
class VectorStore(Protocol):
    """SPI for vector database adapters. Required for Tier 1."""

    SPI_VERSION: ClassVar[int] = 1

    async def store_vectors(self, items: list[VectorItem]) -> list[str]:
        """Store vectors with metadata. Returns stored IDs."""
        ...

    async def search_similar(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int = 10,
        filters: VectorFilters | None = None,
    ) -> list[VectorHit]:
        """Find similar vectors. Returns hits sorted by similarity descending."""
        ...

    async def delete(self, ids: list[str], bank_id: str) -> int:
        """Delete vectors by ID. Returns count deleted."""
        ...

    async def list_vectors(
        self,
        bank_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> list[VectorItem]:
        """List vectors in a bank with pagination. Used by consolidation.

        Returns up to ``limit`` vectors starting at ``offset``.
        Implementations should return vectors in a stable order (e.g., by ID).
        """
        ...

    async def health(self) -> HealthStatus:
        """Check database connectivity."""
        ...


@runtime_checkable
class GraphStore(Protocol):
    """SPI for graph database adapters. Optional for Tier 1."""

    SPI_VERSION: ClassVar[int] = 1

    async def store_entities(self, entities: list[Entity], bank_id: str) -> list[str]:
        """Store or update entities. Returns entity IDs."""
        ...

    async def store_links(self, links: list[EntityLink], bank_id: str) -> list[str]:
        """Store relationships between entities. Returns link IDs."""
        ...

    async def link_memories_to_entities(
        self,
        associations: list[MemoryEntityAssociation],
        bank_id: str,
    ) -> None:
        """Associate memory IDs with entity IDs."""
        ...

    async def query_neighbors(
        self,
        entity_ids: list[str],
        bank_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> list[GraphHit]:
        """Find memories connected to given entities via links."""
        ...

    async def query_entities(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
    ) -> list[Entity]:
        """Search entities by name or alias."""
        ...

    async def health(self) -> HealthStatus:
        """Check database connectivity."""
        ...


@runtime_checkable
class DocumentStore(Protocol):
    """SPI for document storage with full-text search. Optional for Tier 1."""

    SPI_VERSION: ClassVar[int] = 1

    async def store_document(self, document: Document, bank_id: str) -> str:
        """Store a source document. Returns document ID."""
        ...

    async def search_fulltext(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
        filters: DocumentFilters | None = None,
    ) -> list[DocumentHit]:
        """BM25 full-text search over stored content."""
        ...

    async def get_document(self, document_id: str, bank_id: str) -> Document | None:
        """Retrieve a document by ID."""
        ...

    async def health(self) -> HealthStatus:
        """Check database connectivity."""
        ...


# ---------------------------------------------------------------------------
# Tier 2: Engine Provider
# ---------------------------------------------------------------------------


@runtime_checkable
class EngineProvider(Protocol):
    """SPI that full-stack memory engines implement."""

    SPI_VERSION: ClassVar[int] = 1

    def capabilities(self) -> EngineCapabilities:
        """Declare what this engine supports. Called once at init."""
        ...

    async def health(self) -> HealthStatus:
        """Liveness and readiness check."""
        ...

    async def retain(self, request: RetainRequest) -> RetainResult:
        """Store content into memory."""
        ...

    async def recall(self, request: RecallRequest) -> RecallResult:
        """Retrieve relevant memories for a query."""
        ...

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        """Synthesize an answer from memory. Optional — check capabilities."""
        ...

    async def forget(self, request: ForgetRequest) -> ForgetResult:
        """Remove or archive memories. Optional — check capabilities."""
        ...


# ---------------------------------------------------------------------------
# LLM Provider
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """SPI for LLM access needed by the Astrocyte core."""

    SPI_VERSION: ClassVar[int] = 1

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        """Generate a text completion."""
        ...

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """Generate embeddings. May raise NotImplementedError for local fallback."""
        ...

    def capabilities(self) -> LLMCapabilities:
        """Declare LLM capabilities."""
        ...


# ---------------------------------------------------------------------------
# Outbound Transport Provider (optional, cross-cutting)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SPI Version Negotiation
# ---------------------------------------------------------------------------

# Supported SPI versions per protocol
_SUPPORTED_VERSIONS: dict[str, set[int]] = {
    "VectorStore": {1},
    "GraphStore": {1},
    "DocumentStore": {1},
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
            f"{protocol_name} SPI version {version} is not supported. "
            f"Supported versions: {sorted(supported)}"
        )
    return version


@runtime_checkable
class OutboundTransportProvider(Protocol):
    """SPI for credential gateways and HTTP/TLS proxies."""

    SPI_VERSION: ClassVar[int] = 1

    def apply(self, ctx: HttpClientContext) -> None:
        """Configure the HTTP client context."""
        ...

    def subprocess_env(self) -> dict[str, str]:
        """Return environment variables for subprocess calls."""
        ...

    def capabilities(self) -> TransportCapabilities:
        """Declare transport capabilities."""
        ...
