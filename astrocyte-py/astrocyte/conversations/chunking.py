"""Turn-aware conversation chunking (Hindsight parity).

Splits a Conversation into chunks suitable for downstream fact
extraction. Key properties:

- **Never splits a single turn** — turn boundaries are inviolable;
  speaker context stays intact within a chunk.
- **Never spans two sessions** — when consecutive turns carry
  different ``metadata['session_id']`` values, the current chunk
  flushes before the new session's turns begin. Each session
  becomes at least one chunk; long sessions split within
  themselves but never across.
- **Greedy fill within a session** — packs as many consecutive
  same-session turns into a chunk as fit under ``max_chars``, then
  starts a new chunk at the next turn (or session boundary).
- **Single-turn overflow** — if a single turn exceeds ``max_chars``
  by itself, it becomes its own chunk (and downstream extraction
  must handle the oversize input). We don't try to split mid-turn
  because doing so loses speaker attribution and corrupts the
  conversation semantics.

Output format: each ``ConversationChunk`` carries the original turns
plus a rendered markdown text representation suitable for feeding to
``memory.retain()`` directly. The rendering uses ``**{role}**:
{content}`` per turn, matching the convention LME/LoCoMo benches and
Hindsight's ``_chunk_conversation`` already use.

Public API:
    chunk_conversation(conversation, max_chars=12_000) -> list[ConversationChunk]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from astrocyte.conversations.types import Conversation, ConversationTurn

# Hindsight's default conversation chunk size is large (~120k chars,
# ~30k tokens) because their fact-extraction model handles long
# contexts. We start more conservatively at 12k chars (~3k tokens) so
# extraction prompts stay within smaller-model context windows. Tunable
# per call.
DEFAULT_MAX_CHARS_PER_CHUNK = 12_000


@dataclass
class ConversationChunk:
    """One chunk of a conversation — consecutive turns under a char budget.

    ``rendered_text`` is the markdown-formatted version suitable for
    passing directly to ``memory.retain(content=...)``. ``turns`` is the
    raw underlying ConversationTurn objects so downstream callers can
    cross-reference IDs / metadata.
    """

    conversation_id: str
    turns: list[ConversationTurn]
    rendered_text: str
    char_count: int
    chunk_index: int = 0
    metadata: dict = field(default_factory=dict)

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def earliest_timestamp(self) -> datetime | None:
        timestamps = [t.timestamp for t in self.turns if t.timestamp is not None]
        return min(timestamps) if timestamps else None

    @property
    def latest_timestamp(self) -> datetime | None:
        timestamps = [t.timestamp for t in self.turns if t.timestamp is not None]
        return max(timestamps) if timestamps else None


# ─── rendering ─────────────────────────────────────────────────────────


def _render_turn(turn: ConversationTurn) -> str:
    """Render one turn as a markdown block: ``**{role}**: {content}``.

    Matches the format LME/LoCoMo bench harnesses already use; matches
    what Hindsight's ``_chunk_conversation`` produces when serializing
    JSON arrays back to text.
    """
    content = turn.content.replace("\r\n", "\n").strip()
    return f"**{turn.role}**: {content}"


def _render_chunk(turns: list[ConversationTurn]) -> str:
    """Join turns with blank lines between them."""
    return "\n\n".join(_render_turn(t) for t in turns)


def _rendered_char_estimate(turns: list[ConversationTurn]) -> int:
    """Cheap estimate of rendered char count without doing the actual render.

    Used in the packing loop to decide if adding one more turn would
    exceed the budget. Worst-case overhead per turn: ``len(role) + 4
    ("**" + ": ") + 2 ("\\n\\n" separator)``.
    """
    return sum(t.char_count() + 2 for t in turns)


# ─── chunking ──────────────────────────────────────────────────────────


_SESSION_SENTINEL = object()


def _session_key(turn: ConversationTurn) -> Any:
    """Return the turn's session marker, or a shared sentinel when absent.

    Turns are grouped into sessions by ``metadata['session_id']``. When a
    turn has no ``session_id``, it shares the sentinel with other
    session-less turns, so conversations that never carry session
    markers behave exactly like the pre-M17 chunker (one big greedy
    pack across the whole conversation).
    """
    if turn.metadata is None:
        return _SESSION_SENTINEL
    val = turn.metadata.get("session_id", _SESSION_SENTINEL)
    return val if val is not None else _SESSION_SENTINEL


def chunk_conversation(
    conversation: Conversation,
    *,
    max_chars: int = DEFAULT_MAX_CHARS_PER_CHUNK,
) -> list[ConversationChunk]:
    """Split a Conversation into chunks at turn boundaries.

    Two boundaries trigger a flush:
      1. **Session boundary** — the next turn's ``metadata['session_id']``
         differs from the current chunk's. A chunk never spans two
         sessions; each session becomes at least one chunk.
      2. **Size boundary within a session** — adding the next turn
         would exceed ``max_chars``.

    A single turn larger than ``max_chars`` becomes its own chunk (we
    don't split mid-turn — that would lose speaker attribution).

    Empty conversation → empty list. Single-turn conversation → single
    chunk (regardless of turn size). Conversations whose turns carry
    no ``session_id`` behave like the pre-session-aware chunker: one
    greedy pack across the whole conversation.
    """
    if not conversation.turns:
        return []

    chunks: list[ConversationChunk] = []
    current: list[ConversationTurn] = []
    current_size = 0
    current_session: Any = _SESSION_SENTINEL
    chunk_index = 0

    def _flush() -> None:
        nonlocal current, current_size, chunk_index
        chunks.append(
            ConversationChunk(
                conversation_id=conversation.id,
                turns=current,
                rendered_text=_render_chunk(current),
                char_count=_rendered_char_estimate(current),
                chunk_index=chunk_index,
            ),
        )
        chunk_index += 1
        current = []
        current_size = 0

    for turn in conversation.turns:
        turn_size = turn.char_count() + 2  # +2 for "\n\n" separator
        turn_session = _session_key(turn)

        if current:
            session_changed = turn_session is not current_session
            size_overflow = current_size + turn_size > max_chars
            if session_changed or size_overflow:
                _flush()

        current.append(turn)
        current_size += turn_size
        current_session = turn_session

    if current:
        _flush()

    return chunks
