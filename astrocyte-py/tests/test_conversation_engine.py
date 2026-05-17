"""M17 Phase 2.5 tests — Conversation Engine.

Hindsight-parity turn-aware ingestion. Tests cover:
  - types (ConversationTurn, Conversation)
  - chunking (chunk_conversation — turn-boundary respect)
  - storage (InMemoryConversationStore)
  - end-to-end smoke
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from astrocyte.conversations import (
    DEFAULT_MAX_CHARS_PER_CHUNK,
    Conversation,
    ConversationTurn,
    InMemoryConversationStore,
    chunk_conversation,
)

# ─── types: ConversationTurn ──────────────────────────────────────────


class TestConversationTurn:
    def test_new_generates_uuid(self) -> None:
        t = ConversationTurn.new(role="user", content="hi")
        assert len(t.id) == 36
        assert t.role == "user"
        assert t.content == "hi"
        assert t.timestamp is None
        assert t.metadata == {}

    def test_custom_role_allowed(self) -> None:
        t = ConversationTurn.new(role="customer", content="x")
        assert t.role == "customer"

    def test_timestamp_preserved(self) -> None:
        now = datetime.now(timezone.utc)
        t = ConversationTurn.new(role="user", content="x", timestamp=now)
        assert t.timestamp == now

    def test_char_count_approximates_rendered(self) -> None:
        t = ConversationTurn.new(role="user", content="hello")
        # "user" (4) + 4 + "hello" (5) = 13
        assert t.char_count() == 13


# ─── types: Conversation ──────────────────────────────────────────────


class TestConversation:
    def test_new_empty(self) -> None:
        c = Conversation.new()
        assert c.turn_count() == 0
        assert c.turns == []

    def test_new_with_turns(self) -> None:
        c = Conversation.new(
            turns=[
                ConversationTurn.new(role="user", content="hi"),
                ConversationTurn.new(role="assistant", content="hello"),
            ]
        )
        assert c.turn_count() == 2

    def test_add_turn(self) -> None:
        c = Conversation.new()
        t = c.add_turn(role="user", content="hi")
        assert c.turn_count() == 1
        assert c.turns[0] is t

    def test_total_chars(self) -> None:
        c = Conversation.new()
        c.add_turn(role="user", content="a")  # 4+4+1=9
        c.add_turn(role="assistant", content="bb")  # 9+4+2=15
        assert c.total_chars() == 9 + 15

    def test_timestamp_helpers(self) -> None:
        c = Conversation.new()
        now = datetime.now(timezone.utc)
        c.add_turn(role="user", content="a", timestamp=now)
        c.add_turn(role="assistant", content="b", timestamp=now + timedelta(seconds=10))
        c.add_turn(role="user", content="c")  # no ts
        assert c.earliest_timestamp == now
        assert c.latest_timestamp == now + timedelta(seconds=10)

    def test_timestamp_none_when_all_turns_lack_ts(self) -> None:
        c = Conversation.new()
        c.add_turn(role="user", content="x")
        assert c.earliest_timestamp is None
        assert c.latest_timestamp is None


# ─── chunking ─────────────────────────────────────────────────────────


class TestChunkConversation:
    def test_empty_conversation_returns_empty_list(self) -> None:
        c = Conversation.new()
        assert chunk_conversation(c) == []

    def test_single_turn_one_chunk(self) -> None:
        c = Conversation.new()
        c.add_turn(role="user", content="hello")
        chunks = chunk_conversation(c)
        assert len(chunks) == 1
        assert chunks[0].turn_count == 1
        assert "user" in chunks[0].rendered_text
        assert "hello" in chunks[0].rendered_text
        assert chunks[0].chunk_index == 0

    def test_all_fit_in_one_chunk(self) -> None:
        c = Conversation.new()
        for _ in range(5):
            c.add_turn(role="user", content="x")
            c.add_turn(role="assistant", content="y")
        chunks = chunk_conversation(c, max_chars=10_000)
        assert len(chunks) == 1
        assert chunks[0].turn_count == 10

    def test_splits_at_turn_boundary(self) -> None:
        """Greedy fill: when adding a turn would exceed max_chars, start new chunk."""
        c = Conversation.new()
        # Each turn ~= 50 chars rendered. Cap at 100 chars per chunk → roughly 2 turns each.
        for i in range(6):
            c.add_turn(role="user", content="filler " * 5)  # ~35 chars
        chunks = chunk_conversation(c, max_chars=100)
        assert len(chunks) > 1
        # Sum of turn counts equals total turns
        assert sum(ch.turn_count for ch in chunks) == 6
        # Chunk indices are sequential 0,1,2,...
        for i, ch in enumerate(chunks):
            assert ch.chunk_index == i

    def test_oversized_single_turn_stands_alone(self) -> None:
        """One turn larger than max_chars becomes its own chunk (we don't split mid-turn)."""
        c = Conversation.new()
        big_content = "x" * 5000
        c.add_turn(role="user", content="small")
        c.add_turn(role="user", content=big_content)  # >> max
        c.add_turn(role="user", content="also small")
        chunks = chunk_conversation(c, max_chars=200)
        # 3 chunks: ["small"], [big], ["also small"]
        assert len(chunks) == 3
        assert chunks[1].turn_count == 1
        assert chunks[1].turns[0].content == big_content

    def test_rendered_text_uses_role_header(self) -> None:
        c = Conversation.new()
        c.add_turn(role="assistant", content="reply here")
        chunks = chunk_conversation(c)
        assert "**assistant**: reply here" in chunks[0].rendered_text

    def test_rendered_separator_between_turns(self) -> None:
        c = Conversation.new()
        c.add_turn(role="user", content="q")
        c.add_turn(role="assistant", content="a")
        chunks = chunk_conversation(c)
        # Turns separated by blank line
        assert "\n\n" in chunks[0].rendered_text

    def test_conversation_id_propagates_to_chunks(self) -> None:
        c = Conversation.new()
        c.add_turn(role="user", content="x")
        chunks = chunk_conversation(c)
        assert chunks[0].conversation_id == c.id

    def test_chunk_timestamps_track_underlying_turns(self) -> None:
        c = Conversation.new()
        t0 = datetime.now(timezone.utc)
        c.add_turn(role="user", content="a", timestamp=t0)
        c.add_turn(role="user", content="b", timestamp=t0 + timedelta(minutes=5))
        chunks = chunk_conversation(c, max_chars=10_000)
        assert chunks[0].earliest_timestamp == t0
        assert chunks[0].latest_timestamp == t0 + timedelta(minutes=5)

    def test_default_max_chars(self) -> None:
        assert DEFAULT_MAX_CHARS_PER_CHUNK == 12_000


# ─── InMemoryConversationStore ────────────────────────────────────────


class TestInMemoryConversationStore:
    @pytest.mark.asyncio
    async def test_save_and_get(self) -> None:
        store = InMemoryConversationStore()
        c = Conversation.new(title="Onboarding")
        c.add_turn(role="user", content="hi")
        c.add_turn(role="assistant", content="hello")
        await store.save_conversation(c)
        loaded = await store.get_conversation(c.id)
        assert loaded is not None
        assert loaded.title == "Onboarding"
        assert loaded.turn_count() == 2

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self) -> None:
        store = InMemoryConversationStore()
        assert await store.get_conversation("nope") is None

    @pytest.mark.asyncio
    async def test_save_replaces_turns(self) -> None:
        store = InMemoryConversationStore()
        c = Conversation.new()
        c.add_turn(role="user", content="v1")
        await store.save_conversation(c)
        c.turns = []  # blow away
        c.add_turn(role="user", content="v2-1")
        c.add_turn(role="user", content="v2-2")
        await store.save_conversation(c)
        loaded = await store.get_conversation(c.id)
        assert loaded is not None
        assert loaded.turn_count() == 2
        assert loaded.turns[0].content == "v2-1"

    @pytest.mark.asyncio
    async def test_list_newest_first(self) -> None:
        store = InMemoryConversationStore()
        c1 = Conversation.new(title="first")
        await store.save_conversation(c1)
        await asyncio.sleep(0.001)
        c2 = Conversation.new(title="second")
        await store.save_conversation(c2)
        listed = await store.list_conversations()
        assert listed[0].title == "second"
        assert listed[1].title == "first"

    @pytest.mark.asyncio
    async def test_list_respects_limit(self) -> None:
        store = InMemoryConversationStore()
        for _ in range(5):
            await store.save_conversation(Conversation.new())
        assert len(await store.list_conversations(limit=3)) == 3

    @pytest.mark.asyncio
    async def test_delete(self) -> None:
        store = InMemoryConversationStore()
        c = Conversation.new()
        c.add_turn(role="user", content="x")
        await store.save_conversation(c)
        await store.delete_conversation(c.id)
        assert await store.get_conversation(c.id) is None

    @pytest.mark.asyncio
    async def test_delete_missing_noop(self) -> None:
        store = InMemoryConversationStore()
        await store.delete_conversation("nope")  # must not raise

    @pytest.mark.asyncio
    async def test_external_mutation_does_not_corrupt(self) -> None:
        """Saving copies turns; later caller mutation doesn't affect stored state."""
        store = InMemoryConversationStore()
        c = Conversation.new()
        c.add_turn(role="user", content="original")
        await store.save_conversation(c)
        c.add_turn(role="user", content="added-after-save")
        loaded = await store.get_conversation(c.id)
        assert loaded is not None
        # The stored copy has the original turn count
        assert loaded.turn_count() == 1


# ─── end-to-end smoke ─────────────────────────────────────────────────


class TestPhase25EndToEnd:
    @pytest.mark.asyncio
    async def test_build_chunk_store_reload(self) -> None:
        # 1. Build conversation
        c = Conversation.new(source_uri="bench://lme/q-abc", title="LME q-abc")
        now = datetime.now(timezone.utc)
        for i in range(8):
            c.add_turn(
                role="user" if i % 2 == 0 else "assistant",
                content=f"turn {i} content of moderate length here please",
                timestamp=now + timedelta(seconds=i * 30),
            )

        # 2. Chunk it
        chunks = chunk_conversation(c, max_chars=300)
        assert len(chunks) > 1  # forced to split
        assert sum(ch.turn_count for ch in chunks) == 8

        # 3. Store + reload
        store = InMemoryConversationStore()
        await store.save_conversation(c)
        loaded = await store.get_conversation(c.id)
        assert loaded is not None
        assert loaded.turn_count() == 8
        assert loaded.source_uri == "bench://lme/q-abc"
        assert loaded.turns[0].role == "user"
        assert loaded.turns[1].role == "assistant"
