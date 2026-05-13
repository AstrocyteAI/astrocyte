{% set class_suffix = {'vector_stores': 'VectorStore', 'graph_stores': 'GraphStore', 'document_stores': 'DocumentStore', 'pageindex_stores': 'PageIndexStore', 'wiki_stores': 'WikiStore', 'mental_model_stores': 'MentalModelStore', 'source_stores': 'SourceStore', 'llm_providers': 'LLMProvider'}[cookiecutter.spi_family] -%}
"""Async {{ cookiecutter.backend_name }} implementation of :class:`~astrocyte.provider.{{ class_suffix }}`.

Method signatures below are the SPI Protocol's required surface. Each
method currently raises ``NotImplementedError``; fill in the body for
your backend. The conformance test in ``tests/test_smoke.py`` will pass
once every required method is implemented and behaves per the contract
in ``docs/_plugins/provider-spi.md``.
"""

from __future__ import annotations

from typing import ClassVar
{% if cookiecutter.spi_family == 'vector_stores' %}
from astrocyte.types import HealthStatus, VectorFilters, VectorHit, VectorItem
{% elif cookiecutter.spi_family == 'graph_stores' %}
from astrocyte.types import GraphEdge, GraphNode, HealthStatus
{% elif cookiecutter.spi_family == 'document_stores' %}
from astrocyte.types import DocumentHit, DocumentItem, HealthStatus
{% elif cookiecutter.spi_family == 'pageindex_stores' %}
from astrocyte.types import (
    HealthStatus,
    PageIndexDocument,
    PageIndexSection,
    PageIndexSectionEntity,
    PageIndexSectionLink,
)
{% elif cookiecutter.spi_family == 'llm_providers' %}
from astrocyte.types import Completion, Embedding, HealthStatus, Message
{% else %}
from astrocyte.types import HealthStatus
{% endif %}

class {{ cookiecutter.backend_name }}{{ class_suffix }}:
    """{{ class_suffix }} backed by {{ cookiecutter.backend_name }}.

    Configuration is passed via ``__init__`` kwargs from the
    ``{{ cookiecutter.spi_family[:-1] }}_config`` block in ``astrocyte.yaml``.
    """

    # Bump on SPI major version changes — Astrocyte reads this at adapter
    # load time to enforce SPI compatibility.
    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        url: str,
        *,
        api_key: str | None = None,
        timeout: float | None = 30.0,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._timeout = timeout
        # TODO: instantiate the backend's async client here.
        # self._client = {{ cookiecutter.backend_name }}AsyncClient(
        #     url=url, api_key=api_key, timeout=timeout
        # )

{% if cookiecutter.spi_family == 'vector_stores' %}
    async def store_vectors(self, items: list[VectorItem]) -> list[str]:
        """Store vectors with metadata. Returns stored IDs."""
        raise NotImplementedError

    async def search_similar(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int = 10,
        filters: VectorFilters | None = None,
    ) -> list[VectorHit]:
        """Find similar vectors. Returns hits sorted by similarity desc."""
        raise NotImplementedError

    async def delete(self, ids: list[str], bank_id: str) -> int:
        """Delete vectors by ID. Returns count deleted."""
        raise NotImplementedError

    async def list_vectors(
        self,
        bank_id: str,
        offset: int = 0,
        limit: int = 100,
        filters: VectorFilters | None = None,
    ) -> list[VectorItem]:
        """List vectors with optional filters."""
        raise NotImplementedError
{% elif cookiecutter.spi_family == 'graph_stores' %}
    async def upsert_node(self, node: GraphNode) -> str:
        """Upsert a node. Returns the node ID."""
        raise NotImplementedError

    async def upsert_edge(self, edge: GraphEdge) -> str:
        """Upsert an edge. Returns the edge ID."""
        raise NotImplementedError

    async def neighbors(
        self, node_id: str, bank_id: str, max_depth: int = 1, limit: int = 100
    ) -> list[GraphNode]:
        """Return nodes reachable from ``node_id`` within ``max_depth`` hops."""
        raise NotImplementedError

    async def delete_node(self, node_id: str, bank_id: str) -> int:
        """Delete a node and its incident edges."""
        raise NotImplementedError
{% elif cookiecutter.spi_family == 'document_stores' %}
    async def store_documents(self, items: list[DocumentItem]) -> list[str]:
        """Index documents for full-text search."""
        raise NotImplementedError

    async def search_documents(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
    ) -> list[DocumentHit]:
        """BM25 / full-text search."""
        raise NotImplementedError

    async def delete(self, ids: list[str], bank_id: str) -> int:
        """Delete documents by ID."""
        raise NotImplementedError
{% elif cookiecutter.spi_family == 'pageindex_stores' %}
    async def save_document(self, document: PageIndexDocument) -> None:
        """Upsert a PageIndex document (keyed on (bank_id, source_id))."""
        raise NotImplementedError

    async def save_sections(
        self,
        bank_id: str,
        document_id: str,
        sections: list[PageIndexSection],
        entities: list[PageIndexSectionEntity],
        links: list[PageIndexSectionLink],
    ) -> None:
        """Atomic-replace all sections / entities / links for one document."""
        raise NotImplementedError

    async def load_document(
        self, bank_id: str, document_id: str
    ) -> PageIndexDocument | None:
        """Load one PageIndex document or None if missing."""
        raise NotImplementedError

    async def load_skeleton(
        self, bank_id: str, document_id: str
    ) -> list[PageIndexSection]:
        """Load sections WITHOUT ``summary_embedding`` (picker doesn't need it)."""
        raise NotImplementedError

    async def search_semantic(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int = 10,
    ) -> list[tuple[str, int, float]]:
        """Cosine search over section ``summary_embedding``."""
        raise NotImplementedError

    async def search_keyword(
        self, query: str, bank_id: str, limit: int = 10
    ) -> list[tuple[str, int, float]]:
        """BM25 over section text."""
        raise NotImplementedError

    async def search_entity(
        self, entity_names: list[str], bank_id: str, limit: int = 10
    ) -> list[tuple[str, int, float]]:
        """Sections matching the named entities."""
        raise NotImplementedError

    async def search_temporal(
        self, start_iso: str, end_iso: str, bank_id: str, limit: int = 10
    ) -> list[tuple[str, int, float]]:
        """Sections whose temporal range overlaps the query window."""
        raise NotImplementedError

    async def expand_section_links(
        self,
        seed: list[tuple[str, int]],
        bank_id: str,
        link_types: list[str] | None = None,
        limit: int = 50,
    ) -> list[tuple[str, int, float]]:
        """Expand from seed sections via section_links (semantic_knn / causal / supersedes / elaborates)."""
        raise NotImplementedError
{% elif cookiecutter.spi_family == 'llm_providers' %}
    async def complete(self, messages: list[Message], **kwargs) -> Completion:
        """Chat completion."""
        raise NotImplementedError

    async def embed(self, texts: list[str], **kwargs) -> list[Embedding]:
        """Embed text into vectors."""
        raise NotImplementedError
{% else %}
    # TODO: implement the Protocol methods for {{ class_suffix }}. See
    # astrocyte-py/astrocyte/provider.py for the full signatures.
{% endif %}
    async def health(self) -> HealthStatus:
        """Report backend health (ping / version / reachability)."""
        raise NotImplementedError
