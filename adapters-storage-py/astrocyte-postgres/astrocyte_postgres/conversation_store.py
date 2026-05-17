"""PostgresConversationStore — Postgres impl of ConversationStore.

Mirrors PostgresDocumentStore's pattern: psycopg async pool, optional
schema bootstrap, narrow upsert / read / delete surface. Schema lives
in migrations 027 (conversations) + 028 (conversation_turns).

Composability invariant: this store does NOT reference any Memory
Engine table. Conversation→Memory references go through opaque
metadata strings, not typed FKs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import psycopg
from astrocyte.conversations.storage import ConversationStore
from astrocyte.conversations.types import Conversation, ConversationTurn
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


class PostgresConversationStore(ConversationStore):
    """Durable ConversationStore on Postgres (migrations 027 + 028)."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        bootstrap_schema: bool = True,
        **kwargs: Any,
    ) -> None:
        self._dsn = dsn or os.environ.get("DATABASE_URL") or os.environ.get("ASTROCYTE_PG_DSN")
        if not self._dsn:
            raise ValueError(
                "PostgresConversationStore requires `dsn` or DATABASE_URL / ASTROCYTE_PG_DSN",
            )
        self._bootstrap_schema = bool(bootstrap_schema)
        self._pool: AsyncConnectionPool | None = None
        self._pool_lock = asyncio.Lock()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────

    async def _ensure_pool(self) -> AsyncConnectionPool:
        async with self._pool_lock:
            if self._pool is None:

                async def configure(conn: psycopg.AsyncConnection) -> None:
                    await conn.execute('SET search_path = public, "$user"')
                    await conn.commit()

                self._pool = AsyncConnectionPool(
                    conninfo=self._dsn,
                    configure=configure,
                    open=False,
                    min_size=1,
                    max_size=10,
                    kwargs={"connect_timeout": 10},
                )
                await self._pool.open()
            return self._pool

    async def close(self) -> None:
        async with self._pool_lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None

    async def _ensure_schema(self, pool: AsyncConnectionPool) -> None:
        if not self._bootstrap_schema or self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_DDL_CONVERSATIONS)
                    await cur.execute(_DDL_CONVERSATION_TURNS)
                    await cur.execute(_INDEXES)
                await conn.commit()
            self._schema_ready = True

    # ── save ──────────────────────────────────────────────────────────

    async def save_conversation(self, conversation: Conversation) -> None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Upsert the conversation row
                await cur.execute(
                    """
                    INSERT INTO astrocyte_conversations
                        (id, source_uri, title, metadata, created_at)
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        source_uri = EXCLUDED.source_uri,
                        title      = EXCLUDED.title,
                        metadata   = EXCLUDED.metadata
                    """,
                    (
                        conversation.id,
                        conversation.source_uri,
                        conversation.title,
                        json.dumps(conversation.metadata),
                        conversation.created_at,
                    ),
                )
                # Replace any prior turns
                await cur.execute(
                    "DELETE FROM astrocyte_conversation_turns WHERE conversation_id = %s",
                    (conversation.id,),
                )
                if conversation.turns:
                    rows = [
                        (
                            t.id,
                            conversation.id,
                            i,
                            t.role,
                            t.content,
                            t.timestamp,
                            json.dumps(t.metadata),
                        )
                        for i, t in enumerate(conversation.turns)
                    ]
                    await cur.executemany(
                        """
                        INSERT INTO astrocyte_conversation_turns
                            (id, conversation_id, turn_index, role, content, "timestamp", metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                        """,
                        rows,
                    )
            await conn.commit()

    # ── reads ─────────────────────────────────────────────────────────

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, source_uri, title, metadata, created_at
                    FROM astrocyte_conversations
                    WHERE id = %s
                    """,
                    (conversation_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                conv = Conversation(
                    id=str(row[0]),
                    turns=[],
                    source_uri=row[1] or "",
                    title=row[2] or "",
                    metadata=row[3] or {},
                    created_at=row[4],
                )
                await cur.execute(
                    """
                    SELECT id, turn_index, role, content, "timestamp", metadata
                    FROM astrocyte_conversation_turns
                    WHERE conversation_id = %s
                    ORDER BY turn_index
                    """,
                    (conversation_id,),
                )
                turn_rows = await cur.fetchall()
        conv.turns = [
            ConversationTurn(
                id=str(r[0]),
                role=r[2] or "user",
                content=r[3] or "",
                timestamp=r[4],
                metadata=r[5] or {},
            )
            for r in turn_rows
        ]
        return conv

    async def list_conversations(self, *, limit: int = 100) -> list[Conversation]:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id FROM astrocyte_conversations
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    (limit,),
                )
                ids = [str(r[0]) for r in await cur.fetchall()]
        # Eager-load turns. For huge conversations this is expensive;
        # callers should pass small `limit` or add an `include_turns=False`
        # variant (Phase 3+ if needed).
        result: list[Conversation] = []
        for cid in ids:
            conv = await self.get_conversation(cid)
            if conv is not None:
                result.append(conv)
        return result

    # ── delete ────────────────────────────────────────────────────────

    async def delete_conversation(self, conversation_id: str) -> None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # FK CASCADE handles the turns
                await cur.execute(
                    "DELETE FROM astrocyte_conversations WHERE id = %s",
                    (conversation_id,),
                )
            await conn.commit()


# ─── inline DDL (mirrors migrations 027+028) ──────────────────────────

_DDL_CONVERSATIONS = """
CREATE TABLE IF NOT EXISTS astrocyte_conversations (
    id              UUID PRIMARY KEY,
    source_uri      TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_DDL_CONVERSATION_TURNS = """
CREATE TABLE IF NOT EXISTS astrocyte_conversation_turns (
    id               UUID PRIMARY KEY,
    conversation_id  UUID NOT NULL REFERENCES astrocyte_conversations(id) ON DELETE CASCADE,
    turn_index       INTEGER NOT NULL,
    role             TEXT NOT NULL,
    content          TEXT NOT NULL,
    "timestamp"      TIMESTAMPTZ,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (conversation_id, turn_index)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS astrocyte_conversations_source_idx
    ON astrocyte_conversations (source_uri)
    WHERE source_uri <> '';
CREATE INDEX IF NOT EXISTS astrocyte_conversations_created_at_idx
    ON astrocyte_conversations (created_at DESC);
CREATE INDEX IF NOT EXISTS astrocyte_conversation_turns_conv_idx
    ON astrocyte_conversation_turns (conversation_id, turn_index);
CREATE INDEX IF NOT EXISTS astrocyte_conversation_turns_role_idx
    ON astrocyte_conversation_turns (conversation_id, role);
CREATE INDEX IF NOT EXISTS astrocyte_conversation_turns_ts_idx
    ON astrocyte_conversation_turns (conversation_id, "timestamp")
    WHERE "timestamp" IS NOT NULL;
"""
