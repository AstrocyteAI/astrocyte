"""Astrocyte Document Engine — PageIndex-style tree-structured documents.

A standalone subsystem for parsing source documents (markdown, PDF, ...)
into hierarchical tree representations. Independent of the Memory Engine
— no shared tables, no foreign keys. Memory Engine never sees a tree;
Document Engine never sees a Memory.

To compose them (most common case), use ``astrocyte.ingest.DocumentIngestor``
which walks a tree and calls a Memory Engine's retain API with per-node
text + opaque metadata. See ``docs/_design/m17-pageindex-ingestion.md``.

Public API:
    from astrocyte.documents import (
        Document, DocumentTree, TreeNode, NodeSummary,
        Parser, ConvertResult, UnsupportedFileTypeError,
        ParserRegistry, MarkdownParser,
        build_markdown_tree,
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
]
