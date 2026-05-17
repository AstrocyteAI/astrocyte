"""make_retrieval_tools() — agent framework integration.

Returns the three PageIndex-parity tools as plain async callables.
No framework-specific schema is baked in — callers add their own
tool descriptor (OpenAI function schema, Anthropic tool definition,
etc.) around these callables.
"""

from __future__ import annotations

from typing import Callable

from astrocyte.documents.retrieval.retriever import DocumentRetriever
from astrocyte.documents.retrieval.types import DocumentInfo, NodeContent, TreeSkeleton


def make_retrieval_tools(retriever: DocumentRetriever) -> list[Callable]:
    """Return the three PageIndex-parity async tool callables.

    Tools:
      get_document_info(doc_id: str)                → DocumentInfo
      get_document_structure(doc_id: str)           → TreeSkeleton
      get_node_content(doc_id: str, node_id: str)   → NodeContent
    """

    async def get_document_info(doc_id: str) -> DocumentInfo:
        return await retriever.get_document_info(doc_id)

    async def get_document_structure(doc_id: str) -> TreeSkeleton:
        return await retriever.get_document_structure(doc_id)

    async def get_node_content(doc_id: str, node_id: str) -> NodeContent:
        return await retriever.get_node_content(doc_id, node_id)

    get_document_info.__name__ = "get_document_info"
    get_document_structure.__name__ = "get_document_structure"
    get_node_content.__name__ = "get_node_content"

    return [get_document_info, get_document_structure, get_node_content]
