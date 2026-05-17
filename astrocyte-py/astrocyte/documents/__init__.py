"""Astrocyte Document Engine — PageIndex-style tree-structured documents.

A standalone subsystem for parsing source documents (markdown, PDF, ...)
into hierarchical tree representations. Independent of the Memory Engine
— no shared tables, no foreign keys. Memory Engine never sees a tree;
Document Engine never sees a Memory.

Three retrieval paths (see docs/_design/document-engine-roadmap.md §2):
  Path 1 (DE → ME):     DocumentIngestor → memory.retain() → memory.recall()
  Path 2 (CE → ME):     ConversationIngestor → memory.retain() → memory.recall()
  Path 3 (DE + CE → ME): both engines → same bank_id → DocumentNavigator or recall()

Public API:
    from astrocyte.documents import (
        # types
        Document, DocumentTree, TreeNode, NodeSummary,
        # parsers
        Parser, ConvertResult, UnsupportedFileTypeError,
        ParserRegistry, MarkdownParser, MarkitdownParser,
        # builders
        build_markdown_tree, AdaptiveSummarizer,
        # storage
        DocumentStore, InMemoryDocumentStore, DocumentNotFoundError,
        # ingestor
        DocumentIngestor,
        # retrieval (tree-search path)
        DocumentRetriever, DocumentNavigator, make_retrieval_tools,
        TreeSkeleton, SkeletonNode, NodeContent, SectionHit,
        DocumentSearchResult, DocumentInfo,
    )
"""

from astrocyte.documents.builders.md_builder import build_markdown_tree
from astrocyte.documents.builders.summarizer import (
    DEFAULT_THRESHOLD_TOKENS,
    PAGEINDEX_SUMMARY_PROMPT,
    AdaptiveSummarizer,
)
from astrocyte.documents.ingestor import DocumentIngestor
from astrocyte.documents.parsers import (
    ConvertResult,
    MarkdownParser,
    Parser,
    ParserRegistry,
    UnsupportedFileTypeError,
)
from astrocyte.documents.parsers.markitdown import MarkitdownParser
from astrocyte.documents.retrieval import (
    DocumentInfo,
    DocumentNavigator,
    DocumentRetriever,
    DocumentSearchResult,
    NodeContent,
    SectionHit,
    SkeletonNode,
    TreeSkeleton,
    make_retrieval_tools,
)
from astrocyte.documents.storage import (
    DocumentNotFoundError,
    DocumentStore,
    InMemoryDocumentStore,
)
from astrocyte.documents.types import (
    Document,
    DocumentTree,
    NodeSummary,
    TreeNode,
)

__all__ = [
    # types
    "Document",
    "DocumentTree",
    "NodeSummary",
    "TreeNode",
    # parsers
    "Parser",
    "ConvertResult",
    "UnsupportedFileTypeError",
    "ParserRegistry",
    "MarkdownParser",
    "MarkitdownParser",
    # builders
    "build_markdown_tree",
    "AdaptiveSummarizer",
    "DEFAULT_THRESHOLD_TOKENS",
    "PAGEINDEX_SUMMARY_PROMPT",
    # storage
    "DocumentStore",
    "InMemoryDocumentStore",
    "DocumentNotFoundError",
    # ingestor (Memory-Engine bridge)
    "DocumentIngestor",
    # retrieval (tree-search path)
    "DocumentRetriever",
    "DocumentNavigator",
    "make_retrieval_tools",
    "DocumentInfo",
    "SkeletonNode",
    "TreeSkeleton",
    "NodeContent",
    "SectionHit",
    "DocumentSearchResult",
]
