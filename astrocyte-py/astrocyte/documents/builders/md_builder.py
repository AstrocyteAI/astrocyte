r"""Markdown → DocumentTree builder.

PageIndex parity (see ``/Users/calvin/AstrocyteAI/PageIndex/pageindex/page_index_md.py``):

- Header regex: ``^(#{1,6})\s+(.+)$`` — all six heading levels become
  nodes.
- Code-block aware: triple-backtick fences are tracked so headings
  inside code samples don't accidentally split sections.
- Each node owns the text from its heading line up to (but not
  including) the next heading at the same-or-shallower level.
- Children attach to the most-recent ancestor with strictly shallower
  depth — the standard markdown nesting rule.

This module ONLY constructs the tree structure. Per-node summarization
is the summarizer's job (Phase 2). The tree returned here has
``summary=None`` on every node.

Public API:
    build_markdown_tree(md_text, document_id) -> DocumentTree
"""

from __future__ import annotations

import re

from astrocyte.documents.types import DocumentTree, TreeNode

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_CODE_FENCE_RE = re.compile(r"^```")


def _extract_node_headers(md_text: str) -> list[tuple[int, int, str]]:
    """Pass 1 of PageIndex's md→tree: locate every header line.

    Returns a list of ``(line_num, depth, title)`` triples in document
    order. ``line_num`` is 1-indexed. Code blocks are excluded so
    headers inside ```fenced``` regions don't appear.
    """
    headers: list[tuple[int, int, str]] = []
    in_code = False
    for i, line in enumerate(md_text.splitlines(), start=1):
        stripped = line.strip()
        if _CODE_FENCE_RE.match(stripped):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not stripped:
            continue
        m = _HEADER_RE.match(stripped)
        if m:
            depth = len(m.group(1))
            title = m.group(2).strip()
            headers.append((i, depth, title))
    return headers


def _build_node_text(
    headers: list[tuple[int, int, str]],
    md_lines: list[str],
    idx: int,
) -> tuple[str, int]:
    """Compute the text body for header at ``headers[idx]``.

    Returns ``(text, end_line)``. The text spans from the header line
    (inclusive, 1-indexed) up to but not including the next header
    line of any depth (PageIndex behavior — each leaf "owns" its body
    until any next header). ``end_line`` is the exclusive end (next
    header's line_num, or total_lines+1 if last).
    """
    start_line = headers[idx][0]  # 1-indexed
    if idx + 1 < len(headers):
        end_line = headers[idx + 1][0]
    else:
        end_line = len(md_lines) + 1
    # md_lines is 0-indexed; convert
    body = "\n".join(md_lines[start_line - 1 : end_line - 1]).strip()
    return body, end_line


def build_markdown_tree(md_text: str, document_id: str) -> DocumentTree:
    """Build a DocumentTree from markdown text.

    Implementation mirrors PageIndex's two-pass approach:

    Pass 1: scan lines, locate every header (depth + title + line_num).
    Pass 2: stack-based tree construction — each header attaches under
    the most-recent ancestor with strictly shallower depth.

    Each node's text body is the lines from its header through the next
    header of any depth, stripped. Roots are nodes whose parent is None
    (i.e., the document started with that depth, no shallower header
    preceded it).

    Edge cases:
    - Markdown with NO headers → returns DocumentTree with one
      synthetic root holding the full text. (Most documents have at
      least one heading; the bench harness for LME/LoCoMo synthesizes
      ``## Session N`` headers, so this fallback rarely fires in
      practice but keeps the contract well-defined.)
    - Empty input → DocumentTree with empty ``roots`` list.
    """
    if not md_text or not md_text.strip():
        return DocumentTree(document_id=document_id, roots=[])

    md_lines = md_text.splitlines()
    headers = _extract_node_headers(md_text)

    if not headers:
        # Headerless input — produce one synthetic root
        synthetic = TreeNode.new(
            parent_id=None,
            depth=1,
            title="(untitled document)",
            text=md_text.strip(),
            line_start=1,
            line_end=len(md_lines),
        )
        return DocumentTree(document_id=document_id, roots=[synthetic])

    # Build all nodes first (no parent assignments yet)
    nodes: list[TreeNode] = []
    for idx, (line_num, depth, title) in enumerate(headers):
        text, end_line = _build_node_text(headers, md_lines, idx)
        nodes.append(
            TreeNode.new(
                parent_id=None,  # filled in by stack pass below
                depth=depth,
                title=title,
                text=text,
                line_start=line_num,
                line_end=end_line - 1,
            ),
        )

    # Stack-based parent assignment. Stack holds (depth, node) pairs of
    # candidate ancestors. For each new node, pop until top has strictly
    # shallower depth; that's the parent. If stack empties → new root.
    roots: list[TreeNode] = []
    stack: list[TreeNode] = []
    for node in nodes:
        while stack and stack[-1].depth >= node.depth:
            stack.pop()
        if stack:
            stack[-1].add_child(node)
        else:
            roots.append(node)
        stack.append(node)

    return DocumentTree(document_id=document_id, roots=roots)
