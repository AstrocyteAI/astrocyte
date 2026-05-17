"""DocumentStore SPI — persist documents + trees.

Abstract base + an in-memory implementation for tests / embedded use.
Postgres impl lives in ``adapters-storage-py/astrocyte-postgres/`` so
the Document Engine doesn't depend on Postgres directly.

The SPI is intentionally narrow:
  - ``save_document(doc, tree=None)`` — upsert document + optionally its tree
  - ``get_document(doc_id)`` — fetch document metadata
  - ``get_tree(doc_id)`` — reconstruct the DocumentTree from stored nodes
  - ``list_documents(limit)`` — paginate; for control-plane / debugging
  - ``delete_document(doc_id)`` — drop document AND its tree

Trees are stored as flat node rows with parent_id FKs (closure-table
style) rather than nested JSON. Reasons:
  - Allows queries like "fetch all nodes at depth 2" without parsing JSON
  - FK constraints catch orphaned nodes at write time
  - Future tree-aware queries (sibling expansion, depth filtering) are SQL
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from astrocyte.documents.types import Document, DocumentTree, TreeNode


class DocumentNotFoundError(Exception):
    """Raised when a requested document_id doesn't exist in the store."""


class DocumentStore(ABC):
    """Persistence SPI for documents + trees."""

    @abstractmethod
    async def save_document(
        self,
        document: Document,
        tree: DocumentTree | None = None,
    ) -> None:
        """Upsert a document. If ``tree`` is provided, also upsert all its nodes.

        Idempotent — calling twice with the same ``document.id`` updates
        rather than duplicates. If a tree is provided AND the document
        already has a stored tree, the existing tree is replaced (all
        old nodes deleted, new nodes inserted).
        """

    @abstractmethod
    async def get_document(self, document_id: str) -> Document | None:
        """Fetch document metadata. Returns None if not found.

        Note: returned ``Document.tree`` is None even if a tree exists
        in storage — use ``get_tree`` to fetch separately. Two-call
        pattern keeps reads cheap when only metadata is needed.
        """

    @abstractmethod
    async def get_tree(self, document_id: str) -> DocumentTree | None:
        """Reconstruct the DocumentTree from stored nodes.

        Returns None if the document has no stored tree (e.g., Document
        saved without one). Returns an empty tree (no roots) if rows
        exist but are malformed (logged).
        """

    @abstractmethod
    async def list_documents(self, *, limit: int = 100) -> list[Document]:
        """List documents in descending created_at order.

        For control-plane and debugging. Pagination is offset-less for
        Phase 2 simplicity — callers should not rely on stable order
        between pages.
        """

    @abstractmethod
    async def delete_document(self, document_id: str) -> None:
        """Delete a document and all its tree nodes.

        No-op if document_id doesn't exist. Tree-node deletion happens
        via FK cascade (Postgres) or explicit removal (InMemory).
        """


# ─── in-memory impl (tests, embedded use) ─────────────────────────────


class InMemoryDocumentStore(DocumentStore):
    """Pure-Python DocumentStore backed by dicts. Not thread-safe.

    Useful for:
      - Unit tests (no DB setup needed)
      - Embedded / CLI use where persistence isn't required
      - Smoke tests of the Document Engine before wiring to Postgres
    """

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        # tree storage: doc_id → list of (TreeNode, depth-first order index)
        # We store the canonical tree object verbatim for the in-memory case.
        self._trees: dict[str, DocumentTree] = {}

    async def save_document(
        self,
        document: Document,
        tree: DocumentTree | None = None,
    ) -> None:
        # store metadata only (tree on Document is detached)
        self._docs[document.id] = Document(
            id=document.id,
            source_uri=document.source_uri,
            content_hash=document.content_hash,
            mime_type=document.mime_type,
            title=document.title,
            created_at=document.created_at,
            tree=None,  # never inline on the metadata record
        )
        if tree is not None:
            # Replace any previous tree
            self._trees[document.id] = tree

    async def get_document(self, document_id: str) -> Document | None:
        return self._docs.get(document_id)

    async def get_tree(self, document_id: str) -> DocumentTree | None:
        return self._trees.get(document_id)

    async def list_documents(self, *, limit: int = 100) -> list[Document]:
        docs = sorted(self._docs.values(), key=lambda d: d.created_at, reverse=True)
        return docs[:limit]

    async def delete_document(self, document_id: str) -> None:
        self._docs.pop(document_id, None)
        self._trees.pop(document_id, None)


# ─── helper: flatten tree to (parent_id, node) rows ───────────────────


def flatten_tree_rows(tree: DocumentTree) -> Iterable[TreeNode]:
    """Yield nodes in pre-order, with parent_id correctly set.

    The Postgres impl uses this to insert nodes in parent-before-child
    order (required for the FK constraint).
    """
    for n in tree.all_nodes():
        yield n
