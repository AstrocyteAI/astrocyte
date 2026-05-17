"""Document Engine tree-search retrieval.

Two components:
  DocumentRetriever  — low-level reads from DocumentStore (no LLM)
  DocumentNavigator  — agentic loop over the retriever (LLM + tree reasoning)

Three agent-callable tools (PageIndex parity):
  get_document_info(doc_id)          → DocumentInfo
  get_document_structure(doc_id)     → TreeSkeleton
  get_node_content(doc_id, node_id)  → NodeContent

Usage:
    from astrocyte.documents.retrieval import (
        DocumentRetriever,
        DocumentNavigator,
        make_retrieval_tools,
    )
"""

from astrocyte.documents.retrieval.navigator import DocumentNavigator
from astrocyte.documents.retrieval.retriever import DocumentRetriever
from astrocyte.documents.retrieval.tools import make_retrieval_tools
from astrocyte.documents.retrieval.types import (
    DocumentInfo,
    DocumentSearchResult,
    NodeContent,
    SectionHit,
    SkeletonNode,
    TreeSkeleton,
)

__all__ = [
    # retrieval classes
    "DocumentRetriever",
    "DocumentNavigator",
    "make_retrieval_tools",
    # types
    "DocumentInfo",
    "SkeletonNode",
    "TreeSkeleton",
    "NodeContent",
    "SectionHit",
    "DocumentSearchResult",
]
