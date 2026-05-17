"""DocumentNavigator — agentic tree-search retrieval.

PageIndex's core retrieval innovation: instead of vector similarity,
an LLM navigates the document tree structure to identify the exact
relevant section.

Agent loop per document:
  1. get_document_structure() → TreeSkeleton (titles + summaries, no text)
  2. LLM: "Which node_ids are most relevant to {query}?" → JSON list
  3. get_node_content() for each identified node
  4. Optional: explore children if the LLM requests deeper navigation
  5. Compile SectionHit[] with breadcrumb + relevance reasoning

Cost: 1-2 LLM calls per document. No embedding at query time.
Same llm_call pattern as AdaptiveSummarizer — injectable, testable
without an OpenAI key.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from astrocyte.documents.retrieval.retriever import DocumentRetriever
from astrocyte.documents.retrieval.types import (
    DocumentSearchResult,
    SectionHit,
    SkeletonNode,
    TreeSkeleton,
)
from astrocyte.documents.storage import DocumentNotFoundError

logger = logging.getLogger(__name__)

LlmCall = Callable[[str], Awaitable[str]]

# ── Prompts ──────────────────────────────────────────────────────────────────

_STRUCTURE_PROMPT = """\
You are navigating a document tree to find sections relevant to a query.

Document: {title}
Query: {query}

Document structure (node_id | title | summary):
{structure_lines}

Select the most relevant sections. Return a JSON object:
  "node_ids": list of node_id strings, most relevant first (max {max_sections})
  "reasoning": one sentence explaining why these sections answer the query

Return ONLY valid JSON. No markdown fences, no extra text.
Example: {{"node_ids": ["abc123", "def456"], "reasoning": "Section 3.2 covers pricing directly."}}
"""

_CHILD_PROMPT = """\
You are drilling into a document section to answer a query more precisely.

Query: {query}
Parent section: "{parent_title}"

Child sections available:
{children_lines}

Which child node_ids should be retrieved? Return JSON:
{{"node_ids": [...], "reasoning": "..."}}
Return ONLY valid JSON.
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_structure(skeleton: TreeSkeleton) -> str:
    lines: list[str] = []
    for node in skeleton.nodes:
        indent = "  " * max(0, node.depth - 1)
        if node.summary:
            snip = node.summary[:80].replace("\n", " ")
            snip = snip + "…" if len(node.summary) > 80 else snip
            lines.append(f"{indent}{node.node_id} | {node.title} | {snip}")
        else:
            lines.append(f"{indent}{node.node_id} | {node.title}")
    return "\n".join(lines)


def _format_children(children: list[SkeletonNode]) -> str:
    lines: list[str] = []
    for child in children:
        if child.summary:
            snip = child.summary[:80].replace("\n", " ")
            snip = snip + "…" if len(child.summary) > 80 else snip
            lines.append(f"{child.node_id} | {child.title} | {snip}")
        else:
            lines.append(f"{child.node_id} | {child.title}")
    return "\n".join(lines)


def _parse_llm_json(text: str) -> dict:
    """Extract a JSON object from LLM response, tolerating markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    return {"node_ids": [], "reasoning": ""}


def _build_breadcrumb(skeleton: TreeSkeleton, node_id: str) -> list[str]:
    """Return ancestor titles from root to the parent of node_id."""
    parent_map = {n.node_id: n.parent_id for n in skeleton.nodes}
    title_map = {n.node_id: n.title for n in skeleton.nodes}
    chain: list[str] = []
    current = parent_map.get(node_id)
    while current is not None:
        chain.append(title_map.get(current, current))
        current = parent_map.get(current)
    chain.reverse()
    return chain


# ── Navigator ─────────────────────────────────────────────────────────────────

class DocumentNavigator:
    """Agentic tree-search — PageIndex's core retrieval innovation.

    Usage:
        retriever = DocumentRetriever(document_store)
        navigator = DocumentNavigator(retriever, llm_call)
        result = await navigator.search(query, [doc_id])

    ``llm_call`` is ``async def fn(prompt: str) -> str``.
    Pass a real LLM wrapper in production; pass a fake in tests.
    """

    def __init__(
        self,
        retriever: DocumentRetriever,
        llm_call: LlmCall,
        *,
        max_iterations: int = 5,
        max_sections: int = 3,
    ) -> None:
        self._retriever = retriever
        self._llm_call = llm_call
        self.max_iterations = max_iterations
        self.max_sections = max_sections

    async def search(
        self,
        query: str,
        document_ids: list[str],
    ) -> DocumentSearchResult:
        """Tree-search across one or more documents.

        Documents are searched concurrently. Results are ordered by document
        then by LLM relevance ranking within each document.
        """
        tasks = [self._search_one(query, doc_id) for doc_id in document_ids]
        per_doc = await asyncio.gather(*tasks, return_exceptions=True)

        all_sections: list[SectionHit] = []
        iterations_used = 0
        for doc_id, outcome in zip(document_ids, per_doc):
            if isinstance(outcome, Exception):
                logger.warning("tree-search failed for doc=%s: %s", doc_id, outcome)
                continue
            sections, iters = outcome
            all_sections.extend(sections)
            iterations_used += iters

        return DocumentSearchResult(
            query=query,
            sections=all_sections[: self.max_sections * len(document_ids)],
            documents_searched=len(document_ids),
            iterations_used=iterations_used,
        )

    async def _search_one(
        self,
        query: str,
        doc_id: str,
    ) -> tuple[list[SectionHit], int]:
        try:
            skeleton = await self._retriever.get_document_structure(doc_id)
        except DocumentNotFoundError:
            logger.warning("document not found: %s", doc_id)
            return [], 0

        if not skeleton.nodes:
            return [], 0

        iterations = 0
        sections: list[SectionHit] = []
        collected: set[str] = set()

        # ── Step 1: reason over full skeleton ────────────────────────
        prompt = _STRUCTURE_PROMPT.format(
            title=skeleton.title,
            query=query,
            structure_lines=_format_structure(skeleton),
            max_sections=self.max_sections,
        )
        try:
            response = await self._llm_call(prompt)
            iterations += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("navigator LLM call failed for doc=%s: %s", doc_id, exc)
            return [], iterations

        parsed = _parse_llm_json(response)
        node_ids: list[str] = parsed.get("node_ids", [])
        reasoning: str = parsed.get("reasoning", "")

        # ── Step 2: fetch identified nodes ────────────────────────────
        for node_id in node_ids[: self.max_sections]:
            if node_id in collected:
                continue
            try:
                content = await self._retriever.get_node_content(doc_id, node_id)
            except (KeyError, DocumentNotFoundError) as exc:
                logger.debug("node fetch failed %s/%s: %s", doc_id, node_id, exc)
                continue

            sections.append(
                SectionHit(
                    document_id=doc_id,
                    node_id=node_id,
                    node_title=content.title,
                    node_depth=content.depth,
                    breadcrumb=_build_breadcrumb(skeleton, node_id),
                    text=content.text,
                    relevance_reasoning=reasoning,
                )
            )
            collected.add(node_id)

            # ── Step 3: optional child exploration ───────────────────
            if (
                content.children
                and len(sections) < self.max_sections
                and iterations < self.max_iterations
            ):
                child_prompt = _CHILD_PROMPT.format(
                    query=query,
                    parent_title=content.title,
                    children_lines=_format_children(content.children),
                )
                try:
                    child_response = await self._llm_call(child_prompt)
                    iterations += 1
                    child_parsed = _parse_llm_json(child_response)
                    child_ids: list[str] = child_parsed.get("node_ids", [])
                    child_reasoning: str = child_parsed.get("reasoning", reasoning)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("child exploration LLM failed: %s", exc)
                    child_ids, child_reasoning = [], reasoning

                for child_id in child_ids:
                    if len(sections) >= self.max_sections or child_id in collected:
                        break
                    try:
                        cc = await self._retriever.get_node_content(doc_id, child_id)
                    except (KeyError, DocumentNotFoundError):
                        continue
                    sections.append(
                        SectionHit(
                            document_id=doc_id,
                            node_id=child_id,
                            node_title=cc.title,
                            node_depth=cc.depth,
                            breadcrumb=_build_breadcrumb(skeleton, child_id),
                            text=cc.text,
                            relevance_reasoning=child_reasoning,
                        )
                    )
                    collected.add(child_id)

        return sections, iterations
