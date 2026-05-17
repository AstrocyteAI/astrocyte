"""Conversation Engine data types — ConversationTurn, Conversation, TurnRole.

Hindsight-inspired ordered-turn representation. Plain dataclasses; no
DB coupling, no Memory Engine knowledge. Persisted via the
``ConversationStore`` SPI.

Conversation shape:

    Conversation
      └─ turns: ordered list of ConversationTurn
            ├─ role: "user" | "assistant" | "system" | "tool" | (custom)
            ├─ content: the message text
            ├─ timestamp: when the turn happened (optional)
            └─ metadata: free-form per-turn metadata

Why a separate type from Document/DocumentTree:
  - Conversations are inherently SEQUENTIAL, not hierarchical
  - Speaker context matters and must be preserved across chunking
  - Turn boundaries are the natural chunking unit (vs tree boundaries
    for documents)
  - Bench workloads like LME / LoCoMo are conversations, not documents
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

TurnRole = Literal["user", "assistant", "system", "tool"]
"""Standard roles from chat-API conventions (OpenAI / Anthropic / etc).

Other roles are accepted at the type level (we use ``str`` in the
dataclass, not the Literal, so adapters can pass custom roles like
``"customer"`` / ``"agent"`` without changing the framework).
"""


# ─── ConversationTurn ─────────────────────────────────────────────────


@dataclass
class ConversationTurn:
    """One turn in a conversation.

    ``id`` is generated on construction for cross-reference (e.g., when
    a follow-up turn explicitly cites an earlier one). ``timestamp`` is
    optional — many chat sources don't surface per-turn timestamps and
    the conversation-level created_at is sufficient.
    """

    id: str
    role: str  # "user", "assistant", "system", "tool", or custom
    content: str
    timestamp: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        *,
        role: str,
        content: str,
        timestamp: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationTurn:
        """Construct a turn with a fresh UUID id."""
        return cls(
            id=str(uuid.uuid4()),
            role=role,
            content=content,
            timestamp=timestamp,
            metadata=metadata or {},
        )

    def char_count(self) -> int:
        """Total char count including a header line for the role."""
        # Approximates what we'd serialize: "**{role}**: {content}"
        return len(self.role) + 4 + len(self.content)


# ─── Conversation ─────────────────────────────────────────────────────


@dataclass
class Conversation:
    """An ordered sequence of turns with conversation-level metadata.

    ``source_uri`` identifies the upstream conversation source (e.g.,
    ``"slack://channel-id/thread-ts"``, ``"openai-chat://..."``,
    ``"bench://lme/q-12345"``).
    """

    id: str
    turns: list[ConversationTurn] = field(default_factory=list)
    source_uri: str = ""
    title: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        *,
        turns: list[ConversationTurn] | None = None,
        source_uri: str = "",
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Conversation:
        """Construct a Conversation with a fresh UUID id."""
        return cls(
            id=str(uuid.uuid4()),
            turns=turns or [],
            source_uri=source_uri,
            title=title,
            metadata=metadata or {},
        )

    def turn_count(self) -> int:
        return len(self.turns)

    def total_chars(self) -> int:
        return sum(t.char_count() for t in self.turns)

    def add_turn(
        self,
        *,
        role: str,
        content: str,
        timestamp: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationTurn:
        """Append a turn (in-place) and return it."""
        turn = ConversationTurn.new(
            role=role,
            content=content,
            timestamp=timestamp,
            metadata=metadata,
        )
        self.turns.append(turn)
        return turn

    @property
    def earliest_timestamp(self) -> datetime | None:
        timestamps = [t.timestamp for t in self.turns if t.timestamp is not None]
        return min(timestamps) if timestamps else None

    @property
    def latest_timestamp(self) -> datetime | None:
        timestamps = [t.timestamp for t in self.turns if t.timestamp is not None]
        return max(timestamps) if timestamps else None
