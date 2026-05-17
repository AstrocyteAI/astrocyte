"""ConversationIngestor — bridges Conversation Engine output to Memory Engine retain.

Chunks a ``Conversation`` at turn boundaries (Hindsight-parity via
``chunk_conversation``) and calls a Memory Engine retain function once
per chunk. Each retain call carries:

  - ``content``: the rendered chunk text (``**{role}**: {content}`` per
    turn, separated by blank lines)
  - ``metadata``: opaque dict with source attribution + turn-range
    identifiers + timestamps when available

Public API:
    ingestor = ConversationIngestor(retain=memory_engine.retain_text)
    result = await ingestor.ingest(conversation, bank_id="my-bank")
"""

from __future__ import annotations

import logging
from typing import Any

from astrocyte._ingest_spi import IngestResult, MemoryRetainFn
from astrocyte.conversations.chunking import (
    DEFAULT_MAX_CHARS_PER_CHUNK,
    chunk_conversation,
)
from astrocyte.conversations.types import Conversation

logger = logging.getLogger(__name__)

SOURCE_KIND = "astrocyte.conversations"


class ConversationIngestor:
    """Chunks a Conversation and calls retain() per chunk."""

    def __init__(
        self,
        retain: MemoryRetainFn,
        *,
        max_chars_per_chunk: int = DEFAULT_MAX_CHARS_PER_CHUNK,
    ) -> None:
        self._retain = retain
        self._max_chars = max_chars_per_chunk

    async def ingest(
        self,
        conversation: Conversation,
        *,
        bank_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        """Chunk + emit one retain() per chunk.

        Per-chunk failures are swallowed and logged (one bad chunk
        shouldn't abort the whole ingest); their details surface in
        ``IngestResult.failures``.
        """
        chunks = chunk_conversation(conversation, max_chars=self._max_chars)
        failures: list[dict[str, Any]] = []
        emitted = 0

        base_metadata = {
            "source": SOURCE_KIND,
            "source_conversation_id": conversation.id,
            "source_uri": conversation.source_uri,
            "conversation_title": conversation.title,
            **(extra_metadata or {}),
        }

        for chunk in chunks:
            try:
                await self._retain(
                    bank_id=bank_id,
                    content=chunk.rendered_text,
                    metadata={
                        **base_metadata,
                        "chunk_index": chunk.chunk_index,
                        "turn_count": chunk.turn_count,
                        "turn_ids": [t.id for t in chunk.turns],
                        # Per-turn roles preserved so downstream callers can
                        # derive a section-grain speaker tag. Hindsight-parity:
                        # the speaker filter at retrieval time relies on knowing
                        # which roles appear in each chunk.
                        "turn_roles": [t.role for t in chunk.turns],
                        "earliest_timestamp": (
                            chunk.earliest_timestamp.isoformat() if chunk.earliest_timestamp else None
                        ),
                        "latest_timestamp": (chunk.latest_timestamp.isoformat() if chunk.latest_timestamp else None),
                    },
                )
                emitted += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ConversationIngestor: retain failed for chunk=%d of conv=%s: %s",
                    chunk.chunk_index,
                    conversation.id,
                    exc,
                )
                failures.append(
                    {
                        "chunk_index": chunk.chunk_index,
                        "turn_count": chunk.turn_count,
                        "error": str(exc),
                    }
                )

        return IngestResult(
            bank_id=bank_id,
            source_kind=SOURCE_KIND,
            source_id=conversation.id,
            segments_emitted=emitted,
            failures=failures,
            metadata={
                "turn_count": conversation.turn_count(),
                "chunk_count": len(chunks),
            },
        )
