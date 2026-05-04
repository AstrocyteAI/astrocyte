"""PostgreSQL-backed :class:`SourceStore` (M10).

Implements the dedicated :class:`astrocyte.provider.SourceStore` SPI
backing the three-layer hierarchy ``SourceDocument → SourceChunk →
VectorItem``. Schema is defined by ``migrations/014_source_documents.sql``;
the bootstrap path mirrors it for per-test custom-table-name setups.

Per-tenant aware: every SQL statement routes through ``self._fq()`` so a
single store instance serves multiple tenants when the gateway sets
``_current_schema`` per request.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any, ClassVar

import psycopg
from astrocyte.tenancy import fq_table, get_current_schema
from astrocyte.types import HealthStatus, SourceChunk, SourceDocument
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool


class PostgresSourceStore:
    """First-class source-document + chunk store on PostgreSQL.

    Lifecycle:
    - ``store_document`` is upsert-with-dedup: when ``content_hash`` is
      set and matches an existing live row, returns the existing id
      instead of creating a duplicate.
    - ``store_chunks`` is bulk-insert-with-dedup, same contract per chunk.
    - ``delete_document`` is a soft-delete (sets ``deleted_at``) and the
      schema's ``ON DELETE CASCADE`` carries chunks along when a hard
      delete eventually runs. Vectors that reference the chunks via
      ``chunk_id`` are NOT cascade-deleted (those have their own
      ``forgotten_at`` lifecycle).
    """

    SPI_VERSION: ClassVar[int] = 1

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
                "PostgresSourceStore requires `dsn` in source_store_config "
                "or DATABASE_URL / ASTROCYTE_PG_DSN",
            )
        self._bootstrap_schema = bool(bootstrap_schema)
        self._pool: AsyncConnectionPool | None = None
        self._pool_lock = asyncio.Lock()
        # Per-tenant-schema bootstrap tracking — same pattern as PostgresStore.
        self._bootstrapped_schemas: set[str] = set()
        self._schema_lock = asyncio.Lock()

    def _fq(self, table: str) -> str:
        """Schema-qualify ``table`` using the active tenant context."""
        return fq_table(table)

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
                    min_size=2,
                    max_size=20,
                    kwargs={"connect_timeout": 10},
                )
                await self._pool.open()
            return self._pool

    async def _ensure_schema(self, pool: AsyncConnectionPool) -> None:
        """Per-tenant-aware bootstrap. Mirrors 014_source_documents.sql."""
        if not self._bootstrap_schema:
            return
        active_schema = get_current_schema()
        if active_schema in self._bootstrapped_schemas:
            return
        async with self._schema_lock:
            if active_schema in self._bootstrapped_schemas:
                return
            documents = self._fq("astrocyte_source_documents")
            chunks = self._fq("astrocyte_source_chunks")
            async with pool.connection() as conn:
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{active_schema}"')
                # Mirrors 014_source_documents.sql.
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {documents} (
                        id            TEXT       NOT NULL,
                        bank_id       TEXT       NOT NULL,
                        title         TEXT,
                        source_uri    TEXT,
                        content_hash  TEXT,
                        content_type  TEXT,
                        metadata      JSONB      NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        deleted_at    TIMESTAMPTZ,
                        PRIMARY KEY (bank_id, id)
                    )
                    """
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS astrocyte_source_documents_bank_created_idx
                    ON {documents} (bank_id, created_at DESC)
                    WHERE deleted_at IS NULL
                    """
                )
                await conn.execute(
                    f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS astrocyte_source_documents_bank_hash_unique
                    ON {documents} (bank_id, content_hash)
                    WHERE content_hash IS NOT NULL AND deleted_at IS NULL
                    """
                )
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {chunks} (
                        id            TEXT       NOT NULL,
                        bank_id       TEXT       NOT NULL,
                        document_id   TEXT       NOT NULL,
                        chunk_index   INTEGER    NOT NULL,
                        text          TEXT       NOT NULL,
                        content_hash  TEXT,
                        metadata      JSONB      NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (bank_id, id),
                        FOREIGN KEY (bank_id, document_id)
                            REFERENCES {documents}(bank_id, id) ON DELETE CASCADE,
                        UNIQUE (bank_id, document_id, chunk_index)
                    )
                    """
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS astrocyte_source_chunks_doc_order_idx
                    ON {chunks} (bank_id, document_id, chunk_index)
                    """
                )
                await conn.execute(
                    f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS astrocyte_source_chunks_bank_hash_unique
                    ON {chunks} (bank_id, content_hash)
                    WHERE content_hash IS NOT NULL
                    """
                )
                await conn.commit()
            self._bootstrapped_schemas.add(active_schema)

    # ------------------------------------------------------------------
    # SourceStore SPI — documents
    # ------------------------------------------------------------------

    async def store_document(self, document: SourceDocument) -> str:
        """Create document; dedup against existing ``(bank_id, content_hash)``
        when set. Returns the stored / existing id."""
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        documents = self._fq("astrocyte_source_documents")

        # Dedup probe up front: look for an existing live row with the
        # same (bank_id, content_hash). Returns its id without re-storing.
        if document.content_hash:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""
                        SELECT id FROM {documents}
                         WHERE bank_id = %s
                           AND content_hash = %s
                           AND deleted_at IS NULL
                         LIMIT 1
                        """,
                        [document.bank_id, document.content_hash],
                    )
                    existing = await cur.fetchone()
                    if existing is not None:
                        return existing[0]

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    INSERT INTO {documents}
                        (id, bank_id, title, source_uri, content_hash, content_type,
                         metadata, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (bank_id, id) DO UPDATE SET
                        title = EXCLUDED.title,
                        source_uri = EXCLUDED.source_uri,
                        content_hash = EXCLUDED.content_hash,
                        content_type = EXCLUDED.content_type,
                        metadata = EXCLUDED.metadata,
                        deleted_at = NULL
                    """,
                    [
                        document.id,
                        document.bank_id,
                        document.title,
                        document.source_uri,
                        document.content_hash,
                        document.content_type,
                        Json(dict(document.metadata or {})),
                        document.created_at or datetime.now(UTC),
                    ],
                )
            await conn.commit()
        return document.id

    async def get_document(
        self,
        document_id: str,
        bank_id: str,
    ) -> SourceDocument | None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        documents = self._fq("astrocyte_source_documents")
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT id, bank_id, title, source_uri, content_hash,
                           content_type, metadata, created_at
                    FROM {documents}
                    WHERE bank_id = %s AND id = %s AND deleted_at IS NULL
                    """,
                    [bank_id, document_id],
                )
                row = await cur.fetchone()
        return _row_to_document(row) if row else None

    async def find_document_by_hash(
        self,
        content_hash: str,
        bank_id: str,
    ) -> SourceDocument | None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        documents = self._fq("astrocyte_source_documents")
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT id, bank_id, title, source_uri, content_hash,
                           content_type, metadata, created_at
                    FROM {documents}
                    WHERE bank_id = %s AND content_hash = %s AND deleted_at IS NULL
                    LIMIT 1
                    """,
                    [bank_id, content_hash],
                )
                row = await cur.fetchone()
        return _row_to_document(row) if row else None

    async def list_documents(
        self,
        bank_id: str,
        *,
        limit: int = 100,
    ) -> list[SourceDocument]:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        documents = self._fq("astrocyte_source_documents")
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT id, bank_id, title, source_uri, content_hash,
                           content_type, metadata, created_at
                    FROM {documents}
                    WHERE bank_id = %s AND deleted_at IS NULL
                    ORDER BY created_at DESC, id
                    LIMIT %s
                    """,
                    [bank_id, limit],
                )
                rows = await cur.fetchall()
        return [_row_to_document(r) for r in rows]

    async def delete_document(self, document_id: str, bank_id: str) -> bool:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        documents = self._fq("astrocyte_source_documents")
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    UPDATE {documents}
                    SET deleted_at = NOW()
                    WHERE bank_id = %s AND id = %s AND deleted_at IS NULL
                    """,
                    [bank_id, document_id],
                )
                deleted = bool(cur.rowcount)
            await conn.commit()
        return deleted

    # ------------------------------------------------------------------
    # SourceStore SPI — chunks
    # ------------------------------------------------------------------

    async def store_chunks(self, chunks: list[SourceChunk]) -> list[str]:
        """Bulk insert chunks. Per-chunk dedup against
        ``(bank_id, content_hash)`` when set; returns existing id when
        a chunk already exists."""
        if not chunks:
            return []
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        chunks_table = self._fq("astrocyte_source_chunks")

        # Probe content_hash for each chunk before insert. We do this in
        # a single query per call rather than per-chunk to keep it cheap.
        hashes_to_probe = list({
            c.content_hash for c in chunks if c.content_hash and c.bank_id == chunks[0].bank_id
        })
        existing_by_hash: dict[str, str] = {}
        if hashes_to_probe:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""
                        SELECT content_hash, id FROM {chunks_table}
                         WHERE bank_id = %s AND content_hash = ANY(%s::text[])
                        """,
                        [chunks[0].bank_id, hashes_to_probe],
                    )
                    rows = await cur.fetchall()
                    existing_by_hash = {row[0]: row[1] for row in rows}

        now = datetime.now(UTC)
        ids: list[str] = []
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                for chunk in chunks:
                    if chunk.content_hash and chunk.content_hash in existing_by_hash:
                        ids.append(existing_by_hash[chunk.content_hash])
                        continue
                    await cur.execute(
                        f"""
                        INSERT INTO {chunks_table}
                            (id, bank_id, document_id, chunk_index, text,
                             content_hash, metadata, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (bank_id, id) DO UPDATE SET
                            document_id = EXCLUDED.document_id,
                            chunk_index = EXCLUDED.chunk_index,
                            text = EXCLUDED.text,
                            content_hash = EXCLUDED.content_hash,
                            metadata = EXCLUDED.metadata
                        """,
                        [
                            chunk.id,
                            chunk.bank_id,
                            chunk.document_id,
                            chunk.chunk_index,
                            chunk.text,
                            chunk.content_hash,
                            Json(dict(chunk.metadata or {})),
                            chunk.created_at or now,
                        ],
                    )
                    ids.append(chunk.id)
                    if chunk.content_hash:
                        existing_by_hash[chunk.content_hash] = chunk.id
            await conn.commit()
        return ids

    async def get_chunk(self, chunk_id: str, bank_id: str) -> SourceChunk | None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        chunks = self._fq("astrocyte_source_chunks")
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT id, bank_id, document_id, chunk_index, text,
                           content_hash, metadata, created_at
                    FROM {chunks}
                    WHERE bank_id = %s AND id = %s
                    """,
                    [bank_id, chunk_id],
                )
                row = await cur.fetchone()
        return _row_to_chunk(row) if row else None

    async def list_chunks(
        self,
        document_id: str,
        bank_id: str,
    ) -> list[SourceChunk]:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        chunks = self._fq("astrocyte_source_chunks")
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT id, bank_id, document_id, chunk_index, text,
                           content_hash, metadata, created_at
                    FROM {chunks}
                    WHERE bank_id = %s AND document_id = %s
                    ORDER BY chunk_index
                    """,
                    [bank_id, document_id],
                )
                rows = await cur.fetchall()
        return [_row_to_chunk(r) for r in rows]

    async def find_chunk_by_hash(
        self,
        content_hash: str,
        bank_id: str,
    ) -> SourceChunk | None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        chunks = self._fq("astrocyte_source_chunks")
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT id, bank_id, document_id, chunk_index, text,
                           content_hash, metadata, created_at
                    FROM {chunks}
                    WHERE bank_id = %s AND content_hash = %s
                    LIMIT 1
                    """,
                    [bank_id, content_hash],
                )
                row = await cur.fetchone()
        return _row_to_chunk(row) if row else None

    async def health(self) -> HealthStatus:
        try:
            pool = await self._ensure_pool()
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
            return HealthStatus(healthy=True, message="postgres source store connected")
        except Exception as exc:  # pragma: no cover — defensive
            return HealthStatus(healthy=False, message=f"postgres source store unhealthy: {exc!s}")

    async def close(self) -> None:
        async with self._pool_lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


def _row_to_document(row: dict[str, Any]) -> SourceDocument:
    created_at = row["created_at"]
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return SourceDocument(
        id=row["id"],
        bank_id=row["bank_id"],
        title=row["title"],
        source_uri=row["source_uri"],
        content_hash=row["content_hash"],
        content_type=row["content_type"],
        metadata=dict(row["metadata"] or {}),
        created_at=created_at,
    )


def _row_to_chunk(row: dict[str, Any]) -> SourceChunk:
    created_at = row["created_at"]
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return SourceChunk(
        id=row["id"],
        bank_id=row["bank_id"],
        document_id=row["document_id"],
        chunk_index=int(row["chunk_index"]),
        text=row["text"],
        content_hash=row["content_hash"],
        metadata=dict(row["metadata"] or {}),
        created_at=created_at,
    )
