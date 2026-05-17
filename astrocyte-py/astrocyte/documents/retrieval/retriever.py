"""DocumentRetriever — low-level read access to the document tree.

Three PageIndex-parity tools:
  get_document_info(doc_id)           → DocumentInfo
  get_document_structure(doc_id)      → TreeSkeleton  (no text)
  get_node_content(doc_id, node_id)   → NodeContent   (text + children)

No LLM. No agent loop. Directly wraps DocumentStore.
Fully testable without any external service or LLM mock.
"""

from __future__ import annotations

from astrocyte.documents.retrieval.types import (
    DocumentInfo,
    NodeContent,
    SkeletonNode,
    TreeSkeleton,
)
from astrocyte.documents.storage import DocumentNotFoundError, DocumentStore
from astrocyte.documents.types import TreeNode


def _to_skeleton_node(node: TreeNode) -> SkeletonNode:
    return SkeletonNode(
        node_id=node.id,
        parent_id=node.parent_id,
        depth=node.depth,
        title=node.title,
        summary=node.summary.text if node.summary else None,
        has_children=bool(node.children),
        child_count=len(node.children),
        page_start=getattr(node, "page_start", None),
        line_start=node.line_start,
    )


class DocumentRetriever:
    """Read-only document access — three PageIndex-parity tools.

    The low-level layer beneath DocumentNavigator. Consumers can also
    wire these tools directly into their own agent loop via
    make_retrieval_tools().
    """

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    async def get_document_info(self, doc_id: str) -> DocumentInfo:
        """Lightweight metadata — no tree loaded, no text."""
        doc = await self._store.get_document(doc_id)
        if doc is None:
            raise DocumentNotFoundError(doc_id)
        tree = await self._store.get_tree(doc_id)
        nodes = tree.all_nodes() if tree else []
        depths = [n.depth for n in nodes] if nodes else [0]
        return DocumentInfo(
            document_id=doc.id,
            title=doc.title,
            source_uri=doc.source_uri,
            node_count=len(nodes),
            depth_min=min(depths),
            depth_max=max(depths),
            created_at=doc.created_at,
        )

    async def get_document_structure(self, doc_id: str) -> TreeSkeleton:
        """Full tree without node text — the LLM reasoning surface.

        The LLM reads SkeletonNode titles and summaries to decide which
        nodes to retrieve. Raises DocumentNotFoundError if doc_id is
        unknown.
        """
        doc = await self._store.get_document(doc_id)
        if doc is None:
            raise DocumentNotFoundError(doc_id)
        tree = await self._store.get_tree(doc_id)
        if tree is None:
            return TreeSkeleton(document_id=doc_id, title=doc.title, node_count=0)
        nodes_pre = tree.all_nodes()
        return TreeSkeleton(
            document_id=doc_id,
            title=doc.title,
            node_count=len(nodes_pre),
            nodes=[_to_skeleton_node(n) for n in nodes_pre],
        )

    async def get_node_content(self, doc_id: str, node_id: str) -> NodeContent:
        """Full text for one node + its immediate children (skeleton only).

        Raises DocumentNotFoundError if doc_id is unknown.
        Raises KeyError if node_id is not found in the tree.
        """
        tree = await self._store.get_tree(doc_id)
        if tree is None:
            raise DocumentNotFoundError(doc_id)
        node = tree.find(node_id)
        if node is None:
            raise KeyError(f"node_id={node_id!r} not found in document {doc_id!r}")
        return NodeContent(
            node_id=node.id,
            document_id=doc_id,
            title=node.title,
            depth=node.depth,
            parent_id=node.parent_id,
            text=node.text,
            summary=node.summary.text if node.summary else None,
            summary_kind=node.summary.kind if node.summary else None,
            children=[_to_skeleton_node(c) for c in node.children],
            page_start=getattr(node, "page_start", None),
            page_end=getattr(node, "page_end", None),
            line_start=node.line_start,
            line_end=node.line_end,
        )

    async def get_nodes_at_depth(self, doc_id: str, depth: int) -> list[SkeletonNode]:
        """All nodes at a specific depth level (e.g., depth=2 = all H2 sections)."""
        tree = await self._store.get_tree(doc_id)
        if tree is None:
            raise DocumentNotFoundError(doc_id)
        return [_to_skeleton_node(n) for n in tree.all_nodes() if n.depth == depth]
