"""Adaptive per-node summarization (PageIndex parity).

Walks a ``DocumentTree`` and populates each node's ``summary`` field
according to PageIndex's rule:

  - If node text < threshold tokens: ``summary.text = node.text`` (no
    LLM call; ``kind="raw"``).
  - If node text ≥ threshold tokens: call LLM with PageIndex's prompt;
    ``kind="llm"``.
  - For internal nodes (have children): ``kind="prefix"`` — the summary
    is meant to be read as "what this whole subtree is about."

Default threshold: 200 tokens (PageIndex default; configurable).

Public API:
    AdaptiveSummarizer(llm_call, threshold_tokens=200, model="gpt-4o-mini")
        .summarize_tree(tree)        # in-place: sets node.summary on every node
        .summarize_node(node)        # single-node helper

``llm_call`` is a coroutine ``async def fn(prompt: str) -> str``. Pass a
real LLM provider in production; pass a fake in tests. Keeps the
summarizer testable without an OpenAI key.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from astrocyte.documents.types import DocumentTree, NodeSummary, TreeNode
from astrocyte.policy.homeostasis import count_tokens

logger = logging.getLogger(__name__)


# PageIndex parity — the exact prompt from
# /Users/calvin/AstrocyteAI/PageIndex/pageindex/utils.py:generate_node_summary
PAGEINDEX_SUMMARY_PROMPT = """You are given a part of a document, your task is to generate a description of the partial document about what are main points covered in the partial document.

    Partial Document Text: {node_text}

    Directly return the description, do not include any other text.
    """

DEFAULT_THRESHOLD_TOKENS = 200


LlmCall = Callable[[str], Awaitable[str]]


class AdaptiveSummarizer:
    """Per-node summarizer with PageIndex-style adaptive LLM gating.

    Cost shape: most LME / LoCoMo session-blocks are < 200 tokens and
    skip the LLM entirely. LLM is only called for genuinely long nodes
    where the raw text would be too long to use directly downstream.
    """

    def __init__(
        self,
        llm_call: LlmCall,
        *,
        threshold_tokens: int = DEFAULT_THRESHOLD_TOKENS,
        model: str = "gpt-4o-mini",
        max_concurrent_llm: int = 8,
    ) -> None:
        if threshold_tokens < 1:
            raise ValueError(f"threshold_tokens must be >= 1, got {threshold_tokens}")
        self._llm_call = llm_call
        self._threshold = threshold_tokens
        self.model = model
        self._sem = asyncio.Semaphore(max_concurrent_llm)

    # ── single-node helper ────────────────────────────────────────────

    async def summarize_node(self, node: TreeNode) -> NodeSummary:
        """Produce a NodeSummary for a single node.

        Returns the summary (and sets ``node.summary``). Internal-node
        determination is left to the caller because it depends on tree
        traversal context — use ``summarize_tree`` for the typical case.
        """
        text = node.text or ""
        tokens = count_tokens(text)
        if tokens < self._threshold:
            summary = NodeSummary(text=text, kind="raw", token_count=tokens)
            node.summary = summary
            return summary

        async with self._sem:
            try:
                prompt = PAGEINDEX_SUMMARY_PROMPT.format(node_text=text)
                description = await self._llm_call(prompt)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "summarize_node: LLM call failed for node=%s title=%r tokens=%d: %s",
                    node.id,
                    node.title,
                    tokens,
                    exc,
                )
                # Failure mode: degrade to raw text. Downstream still has SOMETHING.
                summary = NodeSummary(text=text, kind="raw", token_count=tokens)
                node.summary = summary
                return summary

        summary_text = (description or "").strip() or text
        summary = NodeSummary(
            text=summary_text,
            kind="llm",
            token_count=count_tokens(summary_text),
        )
        node.summary = summary
        return summary

    # ── tree-level walk ───────────────────────────────────────────────

    async def summarize_tree(self, tree: DocumentTree) -> None:
        """Summarize every node in the tree. Mutates in place.

        Runs node summarizations concurrently (bounded by
        ``max_concurrent_llm``). After completion, each node's
        ``summary`` is populated; internal nodes get ``kind="prefix"``
        re-labeled so callers can distinguish "this is summary of a
        subtree" from "this is summary of a leaf."
        """
        nodes = tree.all_nodes()
        if not nodes:
            return

        # Step 1: summarize every node (concurrent, semaphore-bounded inside)
        await asyncio.gather(*(self.summarize_node(n) for n in nodes))

        # Step 2: re-label internal nodes' summary kind.
        # PageIndex distinguishes prefix_summary (internal) from summary (leaf).
        for n in nodes:
            if n.summary is not None and not n.is_leaf():
                # Promote raw→prefix when an internal node had small text
                # (so its summary is its own heading-and-intro lines) AND
                # promote llm→prefix when it had a generated description.
                # Both indicate "this represents the subtree."
                n.summary = NodeSummary(
                    text=n.summary.text,
                    kind="prefix",
                    token_count=n.summary.token_count,
                )

    # ── introspection ─────────────────────────────────────────────────

    @property
    def threshold_tokens(self) -> int:
        return self._threshold
