"""VectorStore + DocumentStore backed by PostgreSQL with the pgvector extension.

``PostgresStore`` satisfies both the ``VectorStore`` *and* ``DocumentStore``
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
from astrocyte.tenancy import fq_function, fq_table, get_current_schema
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


class PostgresStore:
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
                "PostgresStore requires `dsn` in vector_store_config or DATABASE_URL / ASTROCYTE_PG_DSN",
            )
        self._table = _sanitize_table(table_name)
        self._dim = int(embedding_dimensions)
        if self._dim < 1:
            raise ValueError("embedding_dimensions must be >= 1")
        self._bootstrap_schema = bool(bootstrap_schema)
        self._pool: AsyncConnectionPool | None = None
        self._pool_lock = asyncio.Lock()
        # Per-tenant-schema bootstrap tracking. A single PostgresStore instance
        # can serve multiple tenants when the gateway sets ``_current_schema``
        # per request, so the legacy single-shot ``_schema_ready`` flag has
        # been replaced with a set of schemas that have had bootstrap applied.
        # When ``bootstrap_schema=False`` (production), the set is pre-seeded
        # with every schema the store has been touched from, effectively a
        # no-op.
        self._bootstrapped_schemas: set[str] = set()
        self._schema_lock = asyncio.Lock()

    def _fq(self, table: str | None = None) -> str:
        """Schema-qualify a table name using the current tenant context.

        Defaults to ``self._table`` for the store's primary table; pass an
        explicit name (e.g. ``"astrocyte_banks"``) for the cross-cutting
        helper tables this store also writes to.
        """
        return fq_table(table or self._table)

    def _fq_func(self, function_name: str) -> str:
        """Schema-qualify a function/trigger-function name."""
        return fq_function(function_name)

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
        """Apply the dev/test schema for this store's table_name in the active tenant schema.

        Only runs when ``bootstrap_schema=True`` (the default for tests using
        per-test ``table_name`` strings that migrations cannot pre-create).
        Production sets ``bootstrap_schema=False`` and relies entirely on
        ``migrations/`` applied by ``scripts/migrate.sh`` at deploy time.

        **Per-tenant aware.** A single ``PostgresStore`` instance can serve
        multiple tenants when the gateway sets ``_current_schema`` per
        request; this method tracks which schemas have been bootstrapped and
        runs DDL once per (schema, table_name) pair.

        **Invariant: this method MUST produce the same schema as the SQL
        migrations for ``table_name='astrocyte_vectors'``.** Each DDL block
        below is annotated with the migration file it mirrors. If you add
        DDL here, add the matching migration. If you change the migration,
        update this method.

        ``register_vector_async`` is intentionally NOT called here — the
        pool's per-connection ``configure`` callback handles vector-type
        registration uniformly across both bootstrap modes.
        """
        if not self._bootstrap_schema:
            return
        active_schema = get_current_schema()
        # Cheap fast-path before grabbing the lock.
        if active_schema in self._bootstrapped_schemas:
            return
        async with self._schema_lock:
            if active_schema in self._bootstrapped_schemas:
                return
            # Resolve all qualified names ONCE up front so the giant DDL
            # block below stays readable. Captures `get_current_schema()` at
            # this moment so the whole bootstrap targets the same schema.
            vectors = self._fq()
            banks = self._fq("astrocyte_banks")
            grants = self._fq("astrocyte_bank_access_grants")
            temporal = self._fq("astrocyte_temporal_facts")
            fts_func = self._fq_func(f"{self._table}_fts_update")
            async with pool.connection() as conn:
                # Schemas don't auto-create; create the target schema first
                # if it doesn't exist (no-op for the default ``public``).
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{active_schema}"')
                # Mirrors 001_extension.sql. Extensions live in a single
                # schema cluster-wide; CREATE IF NOT EXISTS is idempotent.
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                # Mirrors 002_astrocytes_vectors.sql (with embedding_dimensions
                # bound to ``self._dim`` instead of psql's :embedding_dimensions
                # variable, plus the lifecycle/layer columns from
                # 004_memory_layer.sql and 006_lifecycle_indexes.sql folded in
                # so a fresh test table needs no follow-up ALTERs).
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {vectors} (
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
                # Mirrors 003_indexes.sql. Index names stay UNqualified —
                # Postgres puts them in the same schema as the indexed table
                # automatically and qualifying them here is a syntax error.
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {self._table}_bank_idx ON {vectors} (bank_id)"
                )
                # Mirrors 006_lifecycle_indexes.sql.
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {self._table}_bank_retained_idx
                    ON {vectors} (bank_id, retained_at DESC)
                    """
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {self._table}_bank_occurred_idx
                    ON {vectors} (bank_id, occurred_at DESC)
                    WHERE occurred_at IS NOT NULL
                    """
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {self._table}_bank_current_idx
                    ON {vectors} (bank_id)
                    WHERE forgotten_at IS NULL
                    """
                )
                # Mirrors 010_hybrid_recall_indexes.sql. Index name aligned
                # with the migration so bootstrap=True and bootstrap=False
                # produce the same schema (was previously diverging:
                # ``..._bank_fact_type_idx`` vs migration ``..._current_idx``).
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {self._table}_bank_fact_type_current_idx
                    ON {vectors} (bank_id, fact_type)
                    WHERE forgotten_at IS NULL
                    """
                )
                # Mirrors 005_banks_access.sql.
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {banks} (
                        id TEXT PRIMARY KEY,
                        tenant_id TEXT,
                        display_name TEXT,
                        description TEXT,
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        archived_at TIMESTAMPTZ
                    )
                    """
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS astrocyte_banks_tenant_idx
                    ON {banks} (tenant_id)
                    WHERE tenant_id IS NOT NULL
                    """
                )
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {grants} (
                        id BIGSERIAL PRIMARY KEY,
                        bank_id TEXT NOT NULL,
                        principal TEXT NOT NULL,
                        permissions TEXT[] NOT NULL,
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        revoked_at TIMESTAMPTZ,
                        UNIQUE (bank_id, principal)
                    )
                    """
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS astrocyte_bank_access_grants_principal_idx
                    ON {grants} (principal)
                    WHERE revoked_at IS NULL
                    """
                )
                # 007_wiki_tables.sql is intentionally NOT mirrored here —
                # those tables are owned by ``astrocyte_postgres.wiki_store``
                # which has its own ``_ensure_schema()``.
                #
                # 009_entities_trigram_embedding.sql is intentionally NOT
                # mirrored here — entity tables are owned by the
                # entity-resolution module and other store adapters.
                #
                # 012_mental_models.sql is intentionally NOT mirrored here —
                # those tables are owned by ``astrocyte_postgres.mental_model_store``
                # which has its own ``_ensure_schema()``.
                #
                # 013_bm25_materialized_view.sql is intentionally NOT mirrored
                # here — the BM25 / IDF materialized views aggregate the WHOLE
                # corpus, so they don't make sense for the per-test custom
                # ``table_name`` setup that drives the bootstrap path. Tests
                # that exercise BM25 must provision the views via migrate.sh
                # against a real ``astrocyte_vectors`` table, then call
                # ``store.refresh_bm25_views()`` to populate them.
                #
                # 014_source_documents.sql is intentionally NOT mirrored here —
                # the source-document/chunk hierarchy is owned by
                # ``astrocyte_postgres.source_store.PostgresSourceStore``, which
                # has its own ``_ensure_schema()``. The ``chunk_id`` column on
                # ``astrocyte_vectors`` is added there too (as a backreference);
                # it's optional and the bootstrap-only path leaves it absent.
                #
                # Mirrors 008_entities_temporal.sql (temporal_facts table only;
                # the entity_* tables in 008 are owned by the entity-resolution
                # adapter, not this store).
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {temporal} (
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
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (bank_id, memory_id, temporal_phrase)
                    )
                    """
                )
                # Mirrors 011_text_fts.sql (BM25/full-text column +
                # GIN index + trigger function + trigger + backfill).
                await conn.execute(
                    f"ALTER TABLE {vectors} ADD COLUMN IF NOT EXISTS text_fts tsvector"
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {self._table}_fts_idx
                    ON {vectors} USING GIN (text_fts)
                    """
                )
                await conn.execute(
                    f"""
                    CREATE OR REPLACE FUNCTION {fts_func}()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        NEW.text_fts := to_tsvector('english', COALESCE(NEW.text, ''));
                        RETURN NEW;
                    END;
                    $$
                    """
                )
                # DROP-then-CREATE so the function body change above takes
                # effect on existing tables (CREATE TRIGGER has no OR REPLACE).
                await conn.execute(
                    f"DROP TRIGGER IF EXISTS {self._table}_fts_trigger ON {vectors}"
                )
                await conn.execute(
                    f"""
                    CREATE TRIGGER {self._table}_fts_trigger
                    BEFORE INSERT OR UPDATE OF text ON {vectors}
                    FOR EACH ROW EXECUTE FUNCTION {fts_func}()
                    """
                )
                # Backfill existing rows that have NULL text_fts.
                await conn.execute(
                    f"""
                    UPDATE {vectors}
                    SET text_fts = to_tsvector('english', COALESCE(text, ''))
                    WHERE text_fts IS NULL
                    """
                )
                await conn.commit()
            self._bootstrapped_schemas.add(active_schema)

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
                        INSERT INTO {self._fq()}
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
            f"""
            INSERT INTO {self._fq("astrocyte_banks")} (id, updated_at)
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
                f"""
                INSERT INTO {self._fq("astrocyte_temporal_facts")}
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
            FROM {self._fq()}
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

    async def search_hybrid_semantic_bm25(
        self,
        query_vector: list[float],
        query: str,
        bank_id: str,
        limit: int = 10,
        filters: VectorFilters | None = None,
    ) -> dict[str, list[VectorHit | DocumentHit]]:
        """Return semantic and BM25 hits with one SQL round trip.

        This is the Postgres-specific Hindsight-style fast path. It preserves
        the public VectorStore/DocumentStore methods as fallbacks while letting
        the native pgvector stack avoid two separate pool checkouts on hot recall.
        """
        if len(query_vector) != self._dim:
            raise ValueError(
                f"Query vector length {len(query_vector)} != embedding_dimensions {self._dim}",
            )
        if not query or not query.strip():
            semantic = await self.search_similar(query_vector, bank_id, limit=limit, filters=filters)
            return {"semantic": semantic, "keyword": []}

        pool = await self._ensure_pool()
        await self._ensure_schema(pool)

        semantic_where = ["bank_id = %s"]
        semantic_params: list[Any] = [bank_id]
        keyword_where = ["bank_id = %s", "text_fts @@ plainto_tsquery('english', %s)"]
        keyword_params: list[Any] = [bank_id, query]

        if filters and filters.as_of:
            semantic_where.append("retained_at <= %s")
            semantic_where.append("(forgotten_at IS NULL OR forgotten_at > %s)")
            semantic_params.extend([filters.as_of, filters.as_of])
            keyword_where.append("retained_at <= %s")
            keyword_where.append("(forgotten_at IS NULL OR forgotten_at > %s)")
            keyword_params.extend([filters.as_of, filters.as_of])
        else:
            semantic_where.append("forgotten_at IS NULL")
            keyword_where.append("forgotten_at IS NULL")

        if filters and filters.tags:
            semantic_where.append("tags && %s::text[]")
            semantic_params.append(filters.tags)
            keyword_where.append("tags && %s::text[]")
            keyword_params.append(filters.tags)

        if filters and filters.fact_types:
            semantic_where.append("fact_type = ANY(%s::text[])")
            semantic_params.append(filters.fact_types)
            keyword_where.append("fact_type = ANY(%s::text[])")
            keyword_params.append(filters.fact_types)

        semantic_sql = " AND ".join(semantic_where)
        keyword_sql = " AND ".join(keyword_where)
        sql = f"""
            WITH semantic AS (
                SELECT
                    'semantic'::text AS strategy,
                    id, text, metadata, tags, fact_type, occurred_at,
                    memory_layer, retained_at,
                    (1 - (embedding <=> %s::vector))::float AS score
                FROM {self._fq()}
                WHERE {semantic_sql}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            ),
            keyword AS (
                SELECT
                    'keyword'::text AS strategy,
                    id, text, metadata, tags, fact_type, occurred_at,
                    memory_layer, retained_at,
                    ts_rank_cd(text_fts, plainto_tsquery('english', %s), 1)::float AS score
                FROM {self._fq()}
                WHERE {keyword_sql}
                ORDER BY score DESC
                LIMIT %s
            )
            SELECT * FROM semantic
            UNION ALL
            SELECT * FROM keyword
        """
        params = [
            query_vector,
            *semantic_params,
            query_vector,
            limit,
            query,
            *keyword_params,
            limit,
        ]

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()

        semantic_hits: list[VectorHit] = []
        keyword_hits: list[DocumentHit] = []
        for row in rows:
            md = row["metadata"]
            if isinstance(md, str):
                md = json.loads(md)
            score = max(0.0, min(1.0, float(row["score"])))
            if row["strategy"] == "semantic":
                semantic_hits.append(
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
            else:
                keyword_hits.append(
                    DocumentHit(
                        document_id=row["id"],
                        text=row["text"],
                        score=score,
                        metadata=md,
                    )
                )

        return {"semantic": semantic_hits, "keyword": keyword_hits}

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
                    FROM {self._fq()}
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

    async def list_recent_vectors(
        self,
        bank_id: str,
        limit: int = 100,
        filters: VectorFilters | None = None,
    ) -> list[VectorItem]:
        """Return recent vectors using Postgres indexes instead of a Python scan."""
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)

        where = ["bank_id = %s"]
        params: list[Any] = [bank_id]
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
        if filters and filters.time_range:
            start, end = filters.time_range
            where.append("occurred_at >= %s")
            where.append("occurred_at <= %s")
            params.extend([start, end])

        params.append(limit)
        where_sql = " AND ".join(where)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT id, bank_id, embedding, text, metadata, tags, fact_type,
                           occurred_at, memory_layer, retained_at
                    FROM {self._fq()}
                    WHERE {where_sql}
                    ORDER BY COALESCE(occurred_at, retained_at) DESC, id
                    LIMIT %s
                    """,
                    params,
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
                    UPDATE {self._fq()}
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

    async def __aenter__(self) -> "PostgresStore":
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
    # PostgresStore satisfies DocumentStore so the recall pipeline can fuse
    # lexical (BM25-style ts_rank) hits alongside semantic (cosine) hits via
    # RRF — exactly the vector+lexical fusion Hindsight uses.  The text is
    # already stored in astrocyte_vectors at retain time; DocumentStore methods
    # operate on the same table via the text_fts tsvector column.

    async def store_document(self, document: Document, bank_id: str) -> str:
        """No-op: text is already stored by store_vectors() at retain time.

        The tsvector trigger keeps text_fts in sync automatically.  This
        method exists to satisfy the DocumentStore protocol so that callers
        (e.g. PipelineOrchestrator) can treat PostgresStore as a
        DocumentStore without a separate code path.
        """
        return document.id

    async def search_fulltext_bm25(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
        filters: DocumentFilters | None = None,
    ) -> list[DocumentHit]:
        """BM25-with-IDF full-text search using the precomputed materialized views.

        Uses the M9 BM25 stack:

        - ``astrocyte_vectors_bm25`` — per-document text_vector + length factor
          (``log(1 + |D|/avgdl_per_bank)``).
        - ``astrocyte_term_idf`` — per-lexeme inverse document frequency
          (``log((N - df + 0.5) / (df + 0.5) + 1)``).

        Both views are materialized so this query path doesn't compete with
        live retain writes for buffer cache, and IDF lookups are O(1) on a
        unique-indexed table instead of an O(corpus) scan per recall.

        Score formula (approximate BM25): ``ts_rank_cd × avg_query_idf /
        doc_length_factor``. ``ts_rank_cd`` provides the term-frequency-
        and-proximity component; ``avg_query_idf`` weights the whole query
        by the rarity of its terms (so "When did **Alice** sign the
        **lease**?" beats "What is the?"); ``doc_length_factor`` keeps long
        documents from dominating short ones for the same hit count.

        Returns the same :class:`DocumentHit` shape as :meth:`search_fulltext`,
        so callers can swap implementations transparently.

        **Refresh requirement**: the materialized views must be refreshed
        for new memories to be searchable. Use :meth:`refresh_bm25_views`
        after batched retain. In production wire it to a scheduled refresh
        (hourly or on-percent-change is reasonable).
        """
        if not query or not query.strip():
            return []

        pool = await self._ensure_pool()
        # We do NOT call ``_ensure_schema`` here — the BM25 views are owned
        # by migration 013 and there's no per-store bootstrap path for
        # them (they aggregate the whole table; bootstrapping per-tenant
        # would require triggering REFRESH at every test setup).

        bm25_view = self._fq("astrocyte_vectors_bm25")
        term_idf_view = self._fq("astrocyte_term_idf")

        where = ["bank_id = %s", "text_vector @@ plainto_tsquery('english', %s)"]
        where_params: list[Any] = [bank_id, query]
        if filters and filters.tags:
            # The MV doesn't carry tags — join back to the source table for
            # the tag filter rather than denormalising into the view.
            where.append(
                f"id IN (SELECT id FROM {self._fq()} WHERE tags && %s::text[])"
            )
            where_params.append(filters.tags)

        where_sql = " AND ".join(where)
        # Aggregate IDF for the query: average of IDFs of (lexemes-of-query)
        # that appear in the corpus. ``regexp_split_to_table`` decomposes
        # the query string into raw words; we lowercase and strip punctuation
        # so the join against ``astrocyte_term_idf.lexeme`` (already
        # lowercase/stemmed) finds matches. Missing terms (queries with
        # words not in the corpus) default the average to 0.5 — small but
        # non-zero so we still get *some* score from ts_rank_cd alone.
        sql = f"""
            WITH query_terms AS (
                SELECT regexp_split_to_table(
                    lower(regexp_replace(%s, '[^a-z0-9 ]', ' ', 'gi')),
                    '\\s+'
                ) AS word
            ),
            query_idf AS (
                SELECT COALESCE(AVG(idf), 0.5) AS avg_idf
                FROM query_terms qt
                JOIN {term_idf_view} ti ON ti.lexeme = qt.word
                WHERE qt.word <> ''
            )
            SELECT
                v.id,
                ts_rank_cd(v.text_vector, plainto_tsquery('english', %s), 0)
                    * (SELECT avg_idf FROM query_idf)
                    / NULLIF(v.doc_length_factor, 0) AS score
            FROM {bm25_view} v
            WHERE {where_sql}
            ORDER BY score DESC NULLS LAST
            LIMIT %s
        """
        params = [query, query, *where_params, limit]

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()

        # The BM25 view doesn't carry text + metadata (would bloat it). For
        # the matching ids, fetch text + metadata from the source table in
        # a single follow-up query.
        ids = [row["id"] for row in rows]
        if not ids:
            return []
        scores_by_id = {row["id"]: float(row["score"] or 0.0) for row in rows}
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT id, text, metadata
                    FROM {self._fq()}
                    WHERE id = ANY(%s::text[]) AND forgotten_at IS NULL
                    """,
                    [ids],
                )
                bodies = {row["id"]: row for row in await cur.fetchall()}

        hits: list[DocumentHit] = []
        for hit_id in ids:
            body = bodies.get(hit_id)
            if body is None:
                # Document was forgotten between MV refresh and now — skip.
                continue
            md = body["metadata"]
            if isinstance(md, str):
                md = json.loads(md)
            hits.append(
                DocumentHit(
                    document_id=body["id"],
                    text=body["text"],
                    score=scores_by_id[hit_id],
                    metadata=md,
                )
            )
        return hits

    async def refresh_bm25_views(self, *, concurrent: bool = True) -> None:
        """Refresh the BM25 / IDF materialized views.

        Call after batched retain so newly-stored memories become
        searchable via :meth:`search_fulltext_bm25`. Bench setups should
        invoke this once between the retain and recall phases.

        Args:
            concurrent: When ``True`` (default), uses ``REFRESH MATERIALIZED
                VIEW CONCURRENTLY`` so reads aren't blocked during the
                refresh. Slower but production-safe. Set ``False`` only for
                a fast initial bulk refresh on an empty/cold view.
        """
        pool = await self._ensure_pool()
        bm25_view = self._fq("astrocyte_vectors_bm25")
        term_idf_view = self._fq("astrocyte_term_idf")
        mode = "CONCURRENTLY " if concurrent else ""
        async with pool.connection() as conn:
            # CONCURRENTLY can't run inside an explicit transaction; psycopg's
            # default autocommit is off, so toggle for the DDL.
            await conn.set_autocommit(True)
            try:
                await conn.execute(f"REFRESH MATERIALIZED VIEW {mode}{bm25_view}")
                # term_idf depends on bm25 (its source query references the
                # other view), so refresh in dependency order.
                await conn.execute(f"REFRESH MATERIALIZED VIEW {mode}{term_idf_view}")
            finally:
                await conn.set_autocommit(False)

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

        For true BM25 ranking with corpus IDF and length normalisation,
        use :meth:`search_fulltext_bm25` (requires migration 013 + periodic
        :meth:`refresh_bm25_views`).
        """
        if not query or not query.strip():
            return []

        pool = await self._ensure_pool()
        await self._ensure_schema(pool)

        where = ["bank_id = %s", "forgotten_at IS NULL", "text_fts @@ plainto_tsquery('english', %s)"]
        where_params: list[Any] = [bank_id, query]

        if filters and filters.tags:
            where.append("tags && %s::text[]")
            where_params.append(filters.tags)

        # SELECT ts_rank_cd(%s) appears before the WHERE %s bindings in the
        # query string, so query must be the first positional param.
        params = [query] + where_params + [limit]

        where_sql = " AND ".join(where)
        sql = f"""
            SELECT
                id,
                text,
                metadata,
                ts_rank_cd(text_fts, plainto_tsquery('english', %s), 1) AS score
            FROM {self._fq()}
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
                    FROM {self._fq()}
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
