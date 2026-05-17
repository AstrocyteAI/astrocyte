"""Retrieval types for the Document Engine tree-search path.

These types form the contract between DocumentRetriever (low-level reads)
and DocumentNavigator (agent loop). They are distinct from DocumentTree /
TreeNode — they are query-time views, not storage representations.

Key design decisions:
- TreeSkeleton never contains node text — prevents accidental large-text
  dumps to the LLM reasoning step.
- SectionHit carries a breadcrumb so callers have structural context
  without having to re-traverse the tree.
- DocumentSearchResult is strategy-tagged so callers can log which
  path produced each result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class DocumentInfo:
    """Lightweight document metadata — no tree, no text."""

    document_id: str
    title: str
    source_uri: str
    node_count: int
    depth_min: int
    depth_max: int
    created_at: datetime | None = None


@dataclass
class SkeletonNode:
    """A tree node stripped of text — for efficient LLM tree reasoning.

    Token budget: ~30-50 tokens per node (title + summary fragment).
    A 100-node document → ~3-5k tokens to transmit the full skeleton.
    """

    node_id: str
    parent_id: str | None
    depth: int
    title: str
    summary: str | None     # node.summary.text if available, else None
    has_children: bool
    child_count: int
    page_start: int | None = None   # for PDFs (1-indexed); None for markdown
    line_start: int | None = None   # for markdown (1-indexed); None for PDFs


@dataclass
class TreeSkeleton:
    """Full tree structure without node text. Pre-order flat list.

    The reasoning surface: the LLM reads titles + summaries to decide
    which nodes to fetch via get_node_content().
    """

    document_id: str
    title: str
    node_count: int
    nodes: list[SkeletonNode] = field(default_factory=list)


@dataclass
class NodeContent:
    """A single node's full content + its immediate children (skeleton form).

    ``children`` are skeleton-only (no text) — the LLM can decide whether
    to drill deeper without paying the cost of fetching all child text.
    """

    node_id: str
    document_id: str
    title: str
    depth: int
    parent_id: str | None
    text: str
    summary: str | None
    summary_kind: str | None    # "raw" | "llm" | "prefix"
    children: list[SkeletonNode] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None


@dataclass
class SectionHit:
    """One relevant section found by the DocumentNavigator."""

    document_id: str
    node_id: str
    node_title: str
    node_depth: int
    breadcrumb: list[str]       # ancestor titles root→parent, e.g. ["Chapter 3", "3.2"]
    text: str
    relevance_reasoning: str    # LLM's explanation of why this section is relevant


@dataclass
class DocumentSearchResult:
    """Aggregated result from DocumentNavigator.search()."""

    query: str
    sections: list[SectionHit]
    documents_searched: int
    iterations_used: int
    strategy: Literal["tree_search"] = "tree_search"
