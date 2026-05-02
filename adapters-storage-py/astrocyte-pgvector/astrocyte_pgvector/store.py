"""VectorStore + DocumentStore backed by PostgreSQL with the pgvector extension.

``PgVectorStore`` satisfies both the ``VectorStore`` *and* ``DocumentStore``
protocols.  The same ``astrocyte_vectors`` table that stores embeddings also
carries a ``text_fts tsvector`` column (GIN-indexed, maintained by a trigger)
so that ``search_fulltext`` runs BM25-style ``ts_rank`` without a separate
Elasticsearch deployment.

This gives the recall pipeline the ``keyword`` strategy for free: when the
gateway or test code resolves ``document_store = pgvector``, ``parallel_retrieve``
can fuse lexical and semantic hits via RRF — exactly as Hindsight does with
its vector+lexical layer.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

import psycopg
from astrocyte.types import (
    Document,
    DocumentFilters,
    DocumentHit,
    HealthStatus,
    VectorFilters,
    VectorHit,
    VectorItem,
)
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

_TABLE_SAFE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _sanitize_table(name: str) -> str:
    if not _TABLE_SAFE.match(name):
        raise ValueError(f"Invalid table name: {name!r}")
    return name


def _split_metadata_list(value: object) -> list[str]:
    if not isinstance(value, str) or not value:
        return []
    return [part for part in value.split("|") if part]


class PgVectorStore:
    """Tier 1 vector store using `pgvector` cosine distance search."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        dsn: str | None = None,
        table_name: str = "astrocyte_vectors",
        embedding_dimensions: int = 128,
        bootstrap_schema: bool = True,
        **kwargs: Any,
    ) -> None:
        self._dsn = dsn or os.environ.get("DATABASE_URL") or os.environ.get("ASTROCYTE_PG_DSN")
        if not self._dsn:
            raise ValueError(
                "PgVectorStore requires `dsn` in vector_store_config or DATABASE_URL / ASTROCYTE_PG_DSN",
            )
        self._table = _sanitize_table(table_name)
        self._dim = int(embedding_dimensions)
        if self._dim < 1:
            raise ValueError("embedding_dimensions must be >= 1")
        self._bootstrap_schema = bool(bootstrap_schema)
        self._pool: AsyncConnectionPool | None = None
        self._pool_lock = asyncio.Lock()
        # When migrations own DDL, skip in-app CREATE TABLE / indexes.
        self._schema_ready = not self._bootstrap_schema
        self._schema_lock = asyncio.Lock()

    async def _ensure_pool(self) -> AsyncConnectionPool:
        async with self._pool_lock:
            if self._pool is None:

                async def configure(conn: psycopg.AsyncConnection) -> None:
                    await conn.execute("SELECT 1")
                    # Pin search_path to ``public`` first so unqualified table
                    # writes/reads (``astrocyte_vectors``, ``astrocyte_banks``,
                    # etc.) always target the canonical migrated tables.
                    # Postgres defaults search_path to ``"$user", public`` which
                    # routes writes to ``<user>.<table>`` if the user-named
                    # schema exists — silently splitting data across schemas
                    # and breaking the entire benchmark when migrations only
                    # ran against ``public``.
                    await conn.execute('SET search_path = public, "$user"')
                    # register_vector_async needs the `vector` type. Skip until pgvector exists (quick path:
                    # /health can run before in-app DDL; runbook path: migrations already created the extension).
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                        ext_present = await cur.fetchone()
                    if ext_present:
                        await register_vector_async(conn)
                    await conn.commit()

                self._pool = AsyncConnectionPool(
                    conninfo=self._dsn,
                    configure=configure,
                    open=False,
                    min_size=2,
                    # Sized for parallel retain + concurrent PgQueuer workers
                    # (e.g. persona-compile tasks each call ``list_vectors``,
                    # ``store_vectors``, etc.). With 10 retain records in
                    # flight and PgQueuer running unbounded persona-compile
                    # jobs in the background, the previous max_size=10
                    # exhausted the pool and triggered cascading
                    # ``PoolTimeout`` errors. 40 leaves ~3 connections of
                    # headroom per concurrent unit.
                    max_size=40,
                    kwargs={"connect_timeout": 10},
                )
                await self._pool.open()
            return self._pool

    async def _ensure_schema(self, pool: AsyncConnectionPool) -> None:
        async with self._schema_lock:
            if self._schema_ready:
                return
            async with pool.connection() as conn:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._table} (
                        id TEXT PRIMARY KEY,
                        bank_id TEXT NOT NULL,
                        embedding vector({self._dim}) NOT NULL,
                        text TEXT NOT NULL,
                        metadata JSONB,
                        tags TEXT[],
                        fact_type TEXT,
                        occurred_at TIMESTAMPTZ,
                        memory_layer TEXT,
                        retained_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        forgotten_at TIMESTAMPTZ
                    )
                    """
                )
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {self._table}_bank_idx ON {self._table} (bank_id)"
                )
                await conn.execute(
                    f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS memory_layer TEXT"
                )
                await conn.execute(
                    f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS retained_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
                )
                await conn.execute(
                    f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS forgotten_at TIMESTAMPTZ"
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS astrocyte_banks (
                        id TEXT PRIMARY KEY,
                        tenant_id TEXT,
                        display_name TEXT,
                        description TEXT,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        archived_at TIMESTAMPTZ
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS astrocyte_bank_access_grants (
                        id BIGSERIAL PRIMARY KEY,
                        bank_id TEXT NOT NULL,
                        principal TEXT NOT NULL,
                        permissions TEXT[] NOT NULL,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        revoked_at TIMESTAMPTZ,
                        UNIQUE (bank_id, principal)
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS astrocyte_temporal_facts (
                        id BIGSERIAL PRIMARY KEY,
                        bank_id TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        temporal_phrase TEXT NOT NULL,
                        anchor_time TIMESTAMPTZ,
                        resolved_start TIMESTAMPTZ,
                        resolved_end TIMESTAMPTZ,
                        resolved_date DATE,
                        date_granularity TEXT,
                        confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (bank_id, memory_id, temporal_phrase)
                    )
                    """
                )
                # ── Full-text search (DocumentStore protocol) ────────────────
                # Add a tsvector column maintained by a trigger so that
                # search_fulltext() can use ts_rank + GIN without a separate
                # Elasticsearch deployment.  All DDL is idempotent: safe to
                # run on an existing table.
                await conn.execute(
                    f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS text_fts tsvector"
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {self._table}_fts_idx
                    ON {self._table} USING GIN (text_fts)
                    """
                )
                # Trigger function (CREATE OR REPLACE is always idempotent).
                await conn.execute(
                    f"""
                    CREATE OR REPLACE FUNCTION {self._table}_fts_update()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        NEW.text_fts := to_tsvector('english', COALESCE(NEW.text, ''));
                        RETURN NEW;
                    END;
                    $$
                    """
                )
                # DROP the trigger first (IF EXISTS) then re-create so the
                # function body change above takes effect on existing tables.
                await conn.execute(
                    f"DROP TRIGGER IF EXISTS {self._table}_fts_trigger ON {self._table}"
                )
                await conn.execute(
                    f"""
                    CREATE TRIGGER {self._table}_fts_trigger
                    BEFORE INSERT OR UPDATE OF text ON {self._table}
                    FOR EACH ROW EXECUTE FUNCTION {self._table}_fts_update()
                    """
                )
                # Backfill existing rows that have NULL text_fts.
                await conn.execute(
                    f"""
                    UPDATE {self._table}
                    SET text_fts = to_tsvector('english', COALESCE(text, ''))
                    WHERE text_fts IS NULL
                    """
                )
                await register_vector_async(conn)
                await conn.commit()
            self._schema_ready = True

    async def store_vectors(self, items: list[VectorItem]) -> list[str]:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        stored: list[str] = []
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                for item in items:
                    if len(item.vector) != self._dim:
                        raise ValueError(
                            f"Vector length {len(item.vector)} != embedding_dimensions {self._dim}",
                        )
                    await self._upsert_bank(cur, item.bank_id)
                    await cur.execute(
                        f"""
                        INSERT INTO {self._table}
                            (
                                id, bank_id, embedding, text, metadata, tags, fact_type,
                                occurred_at, memory_layer, retained_at, forgotten_at
                            )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                        ON CONFLICT (id) DO UPDATE SET
                            bank_id = EXCLUDED.bank_id,
                            embedding = EXCLUDED.embedding,
                            text = EXCLUDED.text,
                            metadata = EXCLUDED.metadata,
                            tags = EXCLUDED.tags,
                            fact_type = EXCLUDED.fact_type,
                            occurred_at = EXCLUDED.occurred_at,
                            memory_layer = EXCLUDED.memory_layer,
                            retained_at = EXCLUDED.retained_at,
                            forgotten_at = NULL
                        """,
                        (
                            item.id,
                            item.bank_id,
                            item.vector,
                            item.text,
                            Json(item.metadata) if item.metadata is not None else None,
                            item.tags,
                            item.fact_type,
                            item.occurred_at,
                            item.memory_layer,
                            item.retained_at or datetime.now(UTC),
                        ),
                    )
                    await self._upsert_temporal_facts(cur, item)
                    stored.append(item.id)
        return stored

    async def _upsert_bank(self, cur: psycopg.AsyncCursor[Any], bank_id: str) -> None:
        await cur.execute(
            """
            INSERT INTO astrocyte_banks (id, updated_at)
            VALUES (%s, NOW())
            ON CONFLICT (id) DO UPDATE SET updated_at = NOW()
            """,
            (bank_id,),
        )

    async def _upsert_temporal_facts(self, cur: psycopg.AsyncCursor[Any], item: VectorItem) -> None:
        metadata = item.metadata or {}
        phrases = _split_metadata_list(metadata.get("temporal_phrase"))
        resolved_dates = _split_metadata_list(metadata.get("resolved_date"))
        granularities = _split_metadata_list(metadata.get("date_granularity"))
        if not phrases or not resolved_dates:
            return
        anchor = metadata.get("temporal_anchor")
        for index, phrase in enumerate(phrases):
            resolved = resolved_dates[index] if index < len(resolved_dates) else resolved_dates[0]
            granularity = granularities[index] if index < len(granularities) else None
            await cur.execute(
                """
                INSERT INTO astrocyte_temporal_facts
                    (bank_id, memory_id, temporal_phrase, anchor_time, resolved_date, date_granularity)
                VALUES (%s, %s, %s, %s::timestamptz, %s::date, %s)
                ON CONFLICT (bank_id, memory_id, temporal_phrase) DO UPDATE SET
                    anchor_time = EXCLUDED.anchor_time,
                    resolved_date = EXCLUDED.resolved_date,
                    date_granularity = EXCLUDED.date_granularity
                """,
                (item.bank_id, item.id, phrase, anchor, resolved, granularity),
            )

    async def search_similar(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int = 10,
        filters: VectorFilters | None = None,
    ) -> list[VectorHit]:
        if len(query_vector) != self._dim:
            raise ValueError(
                f"Query vector length {len(query_vector)} != embedding_dimensions {self._dim}",
            )
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)

        where = ["bank_id = %s"]
        params: list[Any] = [query_vector, bank_id]
        if filters and filters.as_of:
            where.append("retained_at <= %s")
            where.append("(forgotten_at IS NULL OR forgotten_at > %s)")
            params.extend([filters.as_of, filters.as_of])
        else:
            where.append("forgotten_at IS NULL")
        if filters and filters.tags:
            where.append("tags && %s::text[]")
            params.append(filters.tags)
        if filters and filters.fact_types:
            where.append("fact_type = ANY(%s::text[])")
            params.append(filters.fact_types)
        params.extend([query_vector, limit])

        where_sql = " AND ".join(where)
        # Cosine distance `<=>`; map to a 0–1-ish score via (1 - distance).
        sql = f"""
            SELECT id, text, metadata, tags, fact_type, occurred_at, memory_layer, retained_at,
                   (1 - (embedding <=> %s::vector))::float AS score
            FROM {self._table}
            WHERE {where_sql}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()

        hits: list[VectorHit] = []
        for row in rows:
            score = float(row["score"])
            if score < 0.0:
                score = 0.0
            if score > 1.0:
                score = 1.0
            md = row["metadata"]
            if isinstance(md, str):
                md = json.loads(md)
            hits.append(
                VectorHit(
                    id=row["id"],
                    text=row["text"],
                    score=score,
                    metadata=md,
                    tags=list(row["tags"]) if row["tags"] else None,
                    fact_type=row["fact_type"],
                    occurred_at=row["occurred_at"],
                    memory_layer=row.get("memory_layer"),
                    retained_at=row.get("retained_at"),
                )
            )
        return hits

    async def list_vectors(
        self,
        bank_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> list[VectorItem]:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT id, bank_id, embedding, text, metadata, tags, fact_type,
                           occurred_at, memory_layer, retained_at
                    FROM {self._table}
                    WHERE bank_id = %s
                      AND forgotten_at IS NULL
                    ORDER BY id
                    OFFSET %s LIMIT %s
                    """,
                    (bank_id, offset, limit),
                )
                rows = await cur.fetchall()
        items: list[VectorItem] = []
        for row in rows:
            md = row["metadata"]
            if isinstance(md, str):
                md = json.loads(md)
            items.append(
                VectorItem(
                    id=row["id"],
                    bank_id=row["bank_id"],
                    vector=list(row["embedding"]),
                    text=row["text"],
                    metadata=md,
                    tags=list(row["tags"]) if row["tags"] else None,
                    fact_type=row["fact_type"],
                    occurred_at=row["occurred_at"],
                    memory_layer=row.get("memory_layer"),
                    retained_at=row.get("retained_at"),
                )
            )
        return items

    async def delete(self, ids: list[str], bank_id: str) -> int:
        if not ids:
            return 0
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    UPDATE {self._table}
                    SET forgotten_at = NOW()
                    WHERE bank_id = %s
                      AND id = ANY(%s::text[])
                      AND forgotten_at IS NULL
                    """,
                    (bank_id, ids),
                )
                return cur.rowcount or 0

    async def close(self) -> None:
        """Close the connection pool. Safe to call multiple times."""
        async with self._pool_lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None

    async def __aenter__(self) -> "PgVectorStore":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def health(self) -> HealthStatus:
        try:
            pool = await self._ensure_pool()
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
            return HealthStatus(healthy=True, message="pgvector connected")
        except Exception as e:
            return HealthStatus(healthy=False, message=f"pgvector unhealthy: {e!s}")

    # ── DocumentStore protocol ────────────────────────────────────────────────
    # PgVectorStore satisfies DocumentStore so the recall pipeline can fuse
    # lexical (BM25-style ts_rank) hits alongside semantic (cosine) hits via
    # RRF — exactly the vector+lexical fusion Hindsight uses.  The text is
    # already stored in astrocyte_vectors at retain time; DocumentStore methods
    # operate on the same table via the text_fts tsvector column.

    async def store_document(self, document: Document, bank_id: str) -> str:
        """No-op: text is already stored by store_vectors() at retain time.

        The tsvector trigger keeps text_fts in sync automatically.  This
        method exists to satisfy the DocumentStore protocol so that callers
        (e.g. PipelineOrchestrator) can treat PgVectorStore as a
        DocumentStore without a separate code path.
        """
        return document.id

    async def search_fulltext(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
        filters: DocumentFilters | None = None,
    ) -> list[DocumentHit]:
        """BM25-style full-text search using PostgreSQL ts_rank over text_fts.

        Ranks results by ``ts_rank_cd`` (cover-density ranking), which
        rewards query terms appearing close together — a good proxy for BM25
        on the memory-text lengths typical of Astrocyte.  Normalises the raw
        score by document length (``|normalization|=1``) so long memories
        don't dominate short ones.
        """
        if not query or not query.strip():
            return []

        pool = await self._ensure_pool()
        await self._ensure_schema(pool)

        where = ["bank_id = %s", "forgotten_at IS NULL", "text_fts @@ plainto_tsquery('english', %s)"]
        params: list[Any] = [bank_id, query]

        if filters and filters.tags:
            where.append("tags && %s::text[]")
            params.append(filters.tags)

        params.append(query)   # for ts_rank_cd
        params.append(limit)

        where_sql = " AND ".join(where)
        sql = f"""
            SELECT
                id,
                text,
                metadata,
                ts_rank_cd(text_fts, plainto_tsquery('english', %s), 1) AS score
            FROM {self._table}
            WHERE {where_sql}
            ORDER BY score DESC
            LIMIT %s
        """

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()

        hits: list[DocumentHit] = []
        for row in rows:
            md = row["metadata"]
            if isinstance(md, str):
                md = json.loads(md)
            hits.append(
                DocumentHit(
                    document_id=row["id"],
                    text=row["text"],
                    score=float(row["score"]),
                    metadata=md,
                )
            )
        return hits

    async def get_document(self, document_id: str, bank_id: str) -> Document | None:
        """Retrieve a stored memory as a Document by ID."""
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT id, text, metadata, tags
                    FROM {self._table}
                    WHERE id = %s AND bank_id = %s AND forgotten_at IS NULL
                    """,
                    (document_id, bank_id),
                )
                row = await cur.fetchone()

        if row is None:
            return None
        md = row["metadata"]
        if isinstance(md, str):
            md = json.loads(md)
        return Document(
            id=row["id"],
            text=row["text"],
            metadata=md,
            tags=list(row["tags"]) if row["tags"] else None,
        )
