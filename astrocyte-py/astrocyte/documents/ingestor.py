"""DocumentIngestor — bridges Document Engine output to Memory Engine retain.

Walks a ``DocumentTree`` and calls a Memory Engine retain function once
per tree node. Each retain call carries:

  - ``content``: the node's text (or summary if summary_kind='llm' and
    the raw text is too large — adaptive per locked policy)
  - ``metadata``: opaque dict with source attribution + node identifiers

Cross-engine references stay opaque (strings, not FKs). The Memory
Engine doesn't know what a tree is; the Document Engine doesn't know
what a Memory is.

Public API:
    ingestor = DocumentIngestor(retain=memory_engine.retain_text)
    result = await ingestor.ingest(tree, document, bank_id="my-bank")
"""

from __future__ import annotations

import logging
from typing import Any

from astrocyte._ingest_spi import IngestResult, MemoryRetainFn
from astrocyte.documents.types import Document, DocumentTree, TreeNode

logger = logging.getLogger(__name__)

SOURCE_KIND = "astrocyte.documents"

# Beyond this size, prefer the node's summary over its raw text to avoid
# multi-thousand-token retain inputs choking downstream extraction.
# Adaptive summarizer's threshold default (200) is per-node; we use a
# higher value here so most nodes still feed their full text and only
# truly large nodes fall back to summary.
DEFAULT_PREFER_SUMMARY_OVER_CHARS = 4_000


class DocumentIngestor:
    """Walks a DocumentTree and calls retain() per node."""

    def __init__(
        self,
        retain: MemoryRetainFn,
        *,
        prefer_summary_over_chars: int = DEFAULT_PREFER_SUMMARY_OVER_CHARS,
        skip_empty_text: bool = True,
    ) -> None:
        self._retain = retain
        self._prefer_summary_over_chars = prefer_summary_over_chars
        self._skip_empty_text = skip_empty_text

    async def ingest(
        self,
        tree: DocumentTree,
        document: Document,
        *,
        bank_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        """Walk all nodes pre-order; emit one retain() per non-empty node.

        Returns an ``IngestResult`` summarizing what was emitted and any
        per-node failures. Per-node failures are swallowed and logged so
        one bad node doesn't abort the whole ingest.
        """
        failures: list[dict[str, Any]] = []
        emitted = 0
        base_metadata = {
            "source": SOURCE_KIND,
            "source_document_id": document.id,
            "source_uri": document.source_uri,
            "mime_type": document.mime_type,
            **(extra_metadata or {}),
        }

        for node in tree.all_nodes():
            content = self._pick_content(node)
            if self._skip_empty_text and not content.strip():
                continue
            try:
                await self._retain(
                    bank_id=bank_id,
                    content=content,
                    metadata={
                        **base_metadata,
                        "tree_node_id": node.id,
                        "tree_node_parent_id": node.parent_id,
                        "tree_node_depth": node.depth,
                        "tree_node_title": node.title,
                        "tree_node_line_start": node.line_start,
                        "tree_node_line_end": node.line_end,
                        "summary_kind": node.summary.kind if node.summary else None,
                    },
                )
                emitted += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "DocumentIngestor: retain failed for node=%s title=%r: %s",
                    node.id,
                    node.title,
                    exc,
                )
                failures.append(
                    {
                        "tree_node_id": node.id,
                        "title": node.title,
                        "error": str(exc),
                    }
                )

        return IngestResult(
            bank_id=bank_id,
            source_kind=SOURCE_KIND,
            source_id=document.id,
            segments_emitted=emitted,
            failures=failures,
            metadata={"node_count": tree.node_count()},
        )

    # ── content selection ─────────────────────────────────────────────

    def _pick_content(self, node: TreeNode) -> str:
        """Pick the text we feed to the Memory Engine for this node.

        Rule: if the node's raw text is short enough, use it verbatim
        (Memory Engine fact extraction sees full context). If it's
        bigger than ``prefer_summary_over_chars`` AND the summarizer
        produced an LLM-generated summary, use the summary instead
        (avoids feeding multi-thousand-char nodes through downstream
        extraction). Otherwise fall back to raw text — better to feed
        too much than too little.
        """
        text = node.text or ""
        if len(text) <= self._prefer_summary_over_chars:
            return text
        if node.summary is not None and node.summary.kind == "llm":
            return node.summary.text
        return text
