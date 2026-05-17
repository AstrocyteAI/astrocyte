"""Shared SPI types for engine→Memory Engine composition.

Each media-specific engine (Document, Conversation, future Image/Audio)
has its own ingestor that walks a media representation and calls a
Memory Engine retain function with per-chunk text + opaque metadata.
This module defines the shared signature + result shape so ingestors
across engines have a consistent contract.

The Memory Engine itself doesn't import from here — it just accepts
``retain(content, metadata, ...)`` calls. This file is the producer-
side contract; consumers (e.g. ``AstrocyteClient.retain_text``) only
need to match the ``MemoryRetainFn`` signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Protocol


class MemoryRetainFn(Protocol):
    """The minimal Memory Engine retain signature ingestors call.

    Any callable matching this shape can be passed to an ingestor.
    Production: bound method on ``AstrocyteClient``. Tests: in-memory
    list-appending closure.
    """

    def __call__(
        self,
        *,
        bank_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> Awaitable[None]:
        """Retain one segment into the named bank. Returns an awaitable
        that resolves when the segment is persisted. Implementations:
        ``AstrocyteClient.retain_text`` in production, list-appending
        closure in tests."""


@dataclass
class IngestResult:
    """Summary returned by every ingestor after walking a source.

    ``segments_emitted`` is the count of distinct retain calls made
    (nodes for documents, chunks for conversations). ``source_id`` is
    the upstream entity ID (document.id or conversation.id) so callers
    can correlate ingest outcomes back to the source.

    ``failures`` lists any segments whose retain call raised — we
    swallow per-segment failures in the ingestor (one bad chunk
    shouldn't kill the whole ingest) and surface them here.
    """

    bank_id: str
    source_kind: str  # "astrocyte.documents" | "astrocyte.conversations" | ...
    source_id: str
    segments_emitted: int
    failures: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures
