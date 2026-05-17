"""ConversationStore SPI — persist conversations + their turns.

Abstract base + in-memory impl for tests / embedded use. Postgres impl
lives in ``adapters-storage-py/astrocyte-postgres/`` so the
Conversation Engine doesn't depend on Postgres directly.

SPI shape (narrow, mirroring DocumentStore):
  - ``save_conversation(c)`` — upsert conversation + all turns
  - ``get_conversation(id)`` — fetch with all turns
  - ``list_conversations(limit)`` — paginate; control plane / debugging
  - ``delete_conversation(id)`` — drop conversation AND its turns
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from astrocyte.conversations.types import Conversation


class ConversationNotFoundError(Exception):
    """Raised when a requested conversation_id doesn't exist."""


class ConversationStore(ABC):
    """Persistence SPI for conversations + ordered turns."""

    @abstractmethod
    async def save_conversation(self, conversation: Conversation) -> None:
        """Upsert a conversation. Replaces any prior turns (full re-save).

        Idempotent — calling twice with the same ``conversation.id``
        replaces rather than duplicates. The full turn list is
        re-written each call; if you need incremental append, use
        ``append_turns`` (Phase 3+ if needed).
        """

    @abstractmethod
    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Fetch a conversation with all its turns in order. None if not found."""

    @abstractmethod
    async def list_conversations(self, *, limit: int = 100) -> list[Conversation]:
        """List conversations newest-first, with their turns.

        For Phase 2.5 this loads turns eagerly. If conversations grow
        large in production, an ``include_turns=False`` variant is a
        natural follow-on.
        """

    @abstractmethod
    async def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation and all its turns. No-op if not found."""


# ─── in-memory impl ────────────────────────────────────────────────────


class InMemoryConversationStore(ConversationStore):
    """Pure-Python ConversationStore backed by a dict. Not thread-safe.

    For unit tests, CLI/embedded use, smoke tests of the Conversation
    Engine before wiring to Postgres.
    """

    def __init__(self) -> None:
        self._convs: dict[str, Conversation] = {}

    async def save_conversation(self, conversation: Conversation) -> None:
        # Store a copy of the public fields so external mutation of the
        # caller's Conversation object doesn't silently change stored state
        self._convs[conversation.id] = Conversation(
            id=conversation.id,
            turns=list(conversation.turns),  # shallow copy
            source_uri=conversation.source_uri,
            title=conversation.title,
            created_at=conversation.created_at,
            metadata=dict(conversation.metadata),
        )

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._convs.get(conversation_id)

    async def list_conversations(self, *, limit: int = 100) -> list[Conversation]:
        convs = sorted(self._convs.values(), key=lambda c: c.created_at, reverse=True)
        return convs[:limit]

    async def delete_conversation(self, conversation_id: str) -> None:
        self._convs.pop(conversation_id, None)
