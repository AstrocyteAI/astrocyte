"""PostgreSQL-backed first-class :class:`MentalModelStore` (M9).

Implements the dedicated :class:`astrocyte.provider.MentalModelStore`
SPI. Mental models live in their own ``astrocyte_mental_models`` table
with per-revision snapshots in ``astrocyte_mental_model_versions``,
replacing the prior wiki-piggyback design (``WikiPage`` rows with
``kind="concept"`` and ``metadata["_mental_model"] = True``).

Schema is defined by ``migrations/012_mental_models.sql``; the bootstrap
path mirrors it so per-test ``table_name`` setups work without running
migrate.sh first.

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
from astrocyte.types import HealthStatus, MentalModel
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool


class PostgresMentalModelStore:
    """First-class mental-model store backed by PostgreSQL.

    Matches the lifecycle invariants of :class:`MentalModelStore`:

    - ``upsert`` increments revision on each call when the model exists,
      and snapshots the prior revision into ``astrocyte_mental_model_versions``
      so callers can reconstruct history.
    - ``delete`` is a soft-delete (sets ``deleted_at`` on the current row);
      ``get`` and ``list`` filter it out.
    - All queries are tenant-schema-aware via :func:`fq_table`.
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
                "PostgresMentalModelStore requires `dsn` in mental_model_store_config "
                "or DATABASE_URL / ASTROCYTE_PG_DSN",
            )
        self._bootstrap_schema = bool(bootstrap_schema)
        self._pool: AsyncConnectionPool | None = None
        self._pool_lock = asyncio.Lock()
        # Per-tenant-schema bootstrap tracking — same pattern as PostgresStore.
        self._bootstrapped_schemas: set[str] = set()
        self._schema_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Per-tenant naming helpers
    # ------------------------------------------------------------------

    def _fq(self, table: str) -> str:
        """Schema-qualify ``table`` using the active tenant context."""
        return fq_table(table)

    # ------------------------------------------------------------------
    # Connection pool
    # ------------------------------------------------------------------

    async def _ensure_pool(self) -> AsyncConnectionPool:
        async with self._pool_lock:
            if self._pool is None:
                async def configure(conn: psycopg.AsyncConnection) -> None:
                    # Match the wiki/vector pool: pin search_path to ``public``
                    # first so any unqualified type lookups (jsonb, timestamptz)
                    # resolve correctly.
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

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    async def _ensure_schema(self, pool: AsyncConnectionPool) -> None:
        """Per-tenant-aware bootstrap. Mirrors 012_mental_models.sql.

        See :class:`PostgresStore._ensure_schema` for the rationale on
        per-(schema, store) tracking and the bootstrap-vs-migrations
        invariant.
        """
        if not self._bootstrap_schema:
            return
        active_schema = get_current_schema()
        if active_schema in self._bootstrapped_schemas:
            return
        async with self._schema_lock:
            if active_schema in self._bootstrapped_schemas:
                return
            models = self._fq("astrocyte_mental_models")
            versions = self._fq("astrocyte_mental_model_versions")
            async with pool.connection() as conn:
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{active_schema}"')
                # Mirrors 012_mental_models.sql.
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {models} (
                        bank_id      TEXT       NOT NULL,
                        model_id     TEXT       NOT NULL,
                        title        TEXT       NOT NULL,
                        content      TEXT       NOT NULL,
                        scope        TEXT       NOT NULL DEFAULT 'bank',
                        source_ids   TEXT[]     NOT NULL DEFAULT '{{}}'::text[],
                        revision     INTEGER    NOT NULL DEFAULT 1,
                        metadata     JSONB      NOT NULL DEFAULT '{{}}'::jsonb,
                        refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        deleted_at   TIMESTAMPTZ,
                        PRIMARY KEY (bank_id, model_id)
                    )
                    """
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS astrocyte_mental_models_bank_scope_idx
                    ON {models} (bank_id, scope)
                    WHERE deleted_at IS NULL
                    """
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS astrocyte_mental_models_bank_refreshed_idx
                    ON {models} (bank_id, refreshed_at DESC)
                    WHERE deleted_at IS NULL
                    """
                )
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {versions} (
                        id         BIGSERIAL    PRIMARY KEY,
                        bank_id    TEXT         NOT NULL,
                        model_id   TEXT         NOT NULL,
                        revision   INTEGER      NOT NULL,
                        title      TEXT         NOT NULL,
                        content    TEXT         NOT NULL,
                        source_ids TEXT[]       NOT NULL DEFAULT '{{}}'::text[],
                        metadata   JSONB        NOT NULL DEFAULT '{{}}'::jsonb,
                        archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (bank_id, model_id, revision),
                        FOREIGN KEY (bank_id, model_id)
                            REFERENCES {models}(bank_id, model_id) ON DELETE CASCADE
                    )
                    """
                )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS astrocyte_mental_model_versions_lookup_idx
                    ON {versions} (bank_id, model_id, revision DESC)
                    """
                )
                await conn.commit()
            self._bootstrapped_schemas.add(active_schema)

    # ------------------------------------------------------------------
    # MentalModelStore SPI
    # ------------------------------------------------------------------

    async def upsert(self, model: MentalModel, bank_id: str) -> int:
        """Create-or-refresh — bumps revision + archives prior to versions."""
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        models = self._fq("astrocyte_mental_models")
        versions = self._fq("astrocyte_mental_model_versions")
        now = datetime.now(UTC)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                # Look up the current row (if any) to (a) determine the new
                # revision number and (b) archive its prior values into
                # the versions table BEFORE we overwrite. We do this in a
                # single transaction so a concurrent upsert can't observe
                # an intermediate state where the current row has been
                # updated but the prior revision hasn't been archived.
                await cur.execute(
                    f"""
                    SELECT revision, title, content, source_ids, metadata
                    FROM {models}
                    WHERE bank_id = %s AND model_id = %s AND deleted_at IS NULL
                    """,
                    [bank_id, model.model_id],
                )
                existing = await cur.fetchone()
                if existing is None:
                    new_revision = 1
                else:
                    new_revision = int(existing["revision"]) + 1
                    # Archive the row we're about to overwrite.
                    await cur.execute(
                        f"""
                        INSERT INTO {versions}
                            (bank_id, model_id, revision, title, content, source_ids, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (bank_id, model_id, revision) DO NOTHING
                        """,
                        [
                            bank_id,
                            model.model_id,
                            existing["revision"],
                            existing["title"],
                            existing["content"],
                            list(existing["source_ids"] or []),
                            Json(existing["metadata"] or {}),
                        ],
                    )

                # Upsert the current row. ON CONFLICT branches handle both
                # the create and update cases without a second SELECT.
                await cur.execute(
                    f"""
                    INSERT INTO {models}
                        (bank_id, model_id, title, content, scope, source_ids,
                         revision, metadata, refreshed_at, deleted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    ON CONFLICT (bank_id, model_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        scope = EXCLUDED.scope,
                        source_ids = EXCLUDED.source_ids,
                        revision = EXCLUDED.revision,
                        metadata = EXCLUDED.metadata,
                        refreshed_at = EXCLUDED.refreshed_at,
                        deleted_at = NULL
                    """,
                    [
                        bank_id,
                        model.model_id,
                        model.title,
                        model.content,
                        model.scope,
                        list(model.source_ids),
                        new_revision,
                        Json({}),
                        now,
                    ],
                )
            await conn.commit()
        return new_revision

    async def get(self, model_id: str, bank_id: str) -> MentalModel | None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        models = self._fq("astrocyte_mental_models")
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT model_id, bank_id, title, content, scope,
                           source_ids, revision, refreshed_at
                    FROM {models}
                    WHERE bank_id = %s AND model_id = %s AND deleted_at IS NULL
                    """,
                    [bank_id, model_id],
                )
                row = await cur.fetchone()
        return _row_to_model(row) if row else None

    async def list(
        self,
        bank_id: str,
        *,
        scope: str | None = None,
    ) -> list[MentalModel]:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        models = self._fq("astrocyte_mental_models")
        params: list[Any] = [bank_id]
        where = ["bank_id = %s", "deleted_at IS NULL"]
        if scope is not None:
            where.append("scope = %s")
            params.append(scope)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT model_id, bank_id, title, content, scope,
                           source_ids, revision, refreshed_at
                    FROM {models}
                    WHERE {" AND ".join(where)}
                    ORDER BY refreshed_at DESC, model_id
                    """,
                    params,
                )
                rows = await cur.fetchall()
        return [_row_to_model(row) for row in rows]

    async def delete(self, model_id: str, bank_id: str) -> bool:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        models = self._fq("astrocyte_mental_models")
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    UPDATE {models}
                    SET deleted_at = NOW()
                    WHERE bank_id = %s AND model_id = %s AND deleted_at IS NULL
                    """,
                    [bank_id, model_id],
                )
                deleted = bool(cur.rowcount)
            await conn.commit()
        return deleted

    async def health(self) -> HealthStatus:
        try:
            pool = await self._ensure_pool()
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
            return HealthStatus(healthy=True, message="postgres mental model store connected")
        except Exception as exc:  # pragma: no cover — defensive
            return HealthStatus(healthy=False, message=f"postgres mental model store unhealthy: {exc!s}")

    async def close(self) -> None:
        async with self._pool_lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


def _row_to_model(row: dict[str, Any]) -> MentalModel:
    refreshed_at = row["refreshed_at"]
    if refreshed_at.tzinfo is None:
        refreshed_at = refreshed_at.replace(tzinfo=UTC)
    return MentalModel(
        model_id=row["model_id"],
        bank_id=row["bank_id"],
        title=row["title"],
        content=row["content"],
        scope=row["scope"],
        source_ids=list(row["source_ids"] or []),
        revision=int(row["revision"]),
        refreshed_at=refreshed_at,
    )
