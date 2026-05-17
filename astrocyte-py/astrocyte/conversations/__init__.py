"""Astrocyte Conversation Engine — Hindsight-style turn-aware ingestion.

A standalone subsystem for ingesting conversational input (chat
sessions, Slack threads, voice transcripts, multi-turn agent dialogs).
Conversations are inherently sequential — preserved as ordered turns,
chunked at turn boundaries (never mid-turn), with speaker context kept
intact.

Independent of the Document Engine — different input shape, different
chunking strategy. Both feed the same Memory Engine downstream.

To compose them with a memory backend, use ``astrocyte.ingest.ConversationIngestor``
(Phase 3) which walks a conversation and calls memory.retain() per
chunk with opaque metadata.

Public API:
    from astrocyte.conversations import (
        Conversation, ConversationTurn, TurnRole,
        ConversationChunk, chunk_conversation,
        ConversationStore, InMemoryConversationStore,
    )
"""

from astrocyte.conversations.chunking import (
    DEFAULT_MAX_CHARS_PER_CHUNK,
    ConversationChunk,
    chunk_conversation,
)
from astrocyte.conversations.ingestor import ConversationIngestor
from astrocyte.conversations.storage import (
    ConversationNotFoundError,
    ConversationStore,
    InMemoryConversationStore,
)
from astrocyte.conversations.types import (
    Conversation,
    ConversationTurn,
    TurnRole,
)

__all__ = [
    # types
    "Conversation",
    "ConversationTurn",
    "TurnRole",
    # chunking
    "ConversationChunk",
    "chunk_conversation",
    "DEFAULT_MAX_CHARS_PER_CHUNK",
    # storage
    "ConversationStore",
    "InMemoryConversationStore",
    "ConversationNotFoundError",
    # ingestor (Memory-Engine bridge)
    "ConversationIngestor",
]
