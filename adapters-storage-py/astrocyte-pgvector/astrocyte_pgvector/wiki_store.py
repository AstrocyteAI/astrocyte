"""PostgreSQL-backed WikiStore for durable compiled memory pages."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from typing import Any, ClassVar

import psycopg
from astrocyte.types import HealthStatus, WikiPage
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool


class PgWikiStore:
    """Durable WikiStore using the reference Astrocyte Postgres schema."""

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
                "PgWikiStore requires `dsn` in wiki_store_config or DATABASE_URL / ASTROCYTE_PG_DSN",
            )
        self._bootstrap_schema = bool(bootstrap_schema)
        self._pool: AsyncConnectionPool | None = None
        self._pool_lock = asyncio.Lock()
        self._schema_ready = not self._bootstrap_schema
        self._schema_lock = asyncio.Lock()

    async def _ensure_pool(self) -> AsyncConnectionPool:
        async with self._pool_lock:
            if self._pool is None:
                self._pool = AsyncConnectionPool(
                    conninfo=self._dsn,
                    open=False,
                    min_size=1,
                    max_size=10,
                    kwargs={"connect_timeout": 10},
                )
                await self._pool.open()
            return self._pool

    async def _ensure_schema(self, pool: AsyncConnectionPool) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            async with pool.connection() as conn:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
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
                    CREATE TABLE IF NOT EXISTS astrocyte_wiki_pages (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        page_id TEXT NOT NULL,
                        bank_id TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        title TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('topic', 'entity', 'concept')),
                        scope TEXT NOT NULL,
                        current_revision_id UUID,
                        confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                        tags TEXT[],
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        deleted_at TIMESTAMPTZ,
                        UNIQUE (bank_id, page_id),
                        UNIQUE (bank_id, slug)
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS astrocyte_wiki_revisions (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        page_uuid UUID NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
                        revision_number INTEGER NOT NULL,
                        markdown TEXT NOT NULL,
                        summary TEXT,
                        compiled_by TEXT,
                        source_count INTEGER NOT NULL DEFAULT 0,
                        tokens_used INTEGER NOT NULL DEFAULT 0,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (page_uuid, revision_number)
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS astrocyte_wiki_revision_sources (
                        revision_id UUID NOT NULL REFERENCES astrocyte_wiki_revisions(id) ON DELETE CASCADE,
                        memory_id TEXT NOT NULL,
                        bank_id TEXT NOT NULL,
                        quote TEXT,
                        relevance DOUBLE PRECISION,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        PRIMARY KEY (revision_id, memory_id)
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS astrocyte_wiki_links (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        from_page_id UUID NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
                        to_page_id UUID REFERENCES astrocyte_wiki_pages(id) ON DELETE SET NULL,
                        target_slug TEXT NOT NULL,
                        link_type TEXT NOT NULL DEFAULT 'related',
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (from_page_id, target_slug, link_type)
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS astrocyte_wiki_lint_issues (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        page_id UUID NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
                        revision_id UUID REFERENCES astrocyte_wiki_revisions(id) ON DELETE SET NULL,
                        issue_type TEXT NOT NULL,
                        severity TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
                        message TEXT NOT NULL,
                        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
                        status TEXT NOT NULL DEFAULT 'open',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        resolved_at TIMESTAMPTZ
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS astrocyte_wiki_pages_bank_kind_idx
                    ON astrocyte_wiki_pages (bank_id, kind)
                    WHERE deleted_at IS NULL
                    """
                )
                await conn.commit()
            self._schema_ready = True

    async def upsert_page(self, page: WikiPage, bank_id: str) -> str:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        now = datetime.now(UTC)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO astrocyte_banks (id, updated_at)
                    VALUES (%s, NOW())
                    ON CONFLICT (id) DO UPDATE SET updated_at = NOW()
                    """,
                    (bank_id,),
                )
                await cur.execute(
                    """
                    SELECT p.id, COALESCE(MAX(r.revision_number), 0) AS revision_number
                    FROM astrocyte_wiki_pages p
                    LEFT JOIN astrocyte_wiki_revisions r ON r.page_uuid = p.id
                    WHERE p.bank_id = %s AND p.page_id = %s
                    GROUP BY p.id
                    """,
                    (bank_id, page.page_id),
                )
                existing = await cur.fetchone()
                if existing:
                    page_uuid = existing["id"]
                    revision = int(existing["revision_number"]) + 1
                    await cur.execute(
                        """
                        UPDATE astrocyte_wiki_pages
                        SET slug = %s,
                            title = %s,
                            kind = %s,
                            scope = %s,
                            tags = %s,
                            metadata = %s,
                            updated_at = %s,
                            deleted_at = NULL
                        WHERE id = %s
                        """,
                        (
                            _slug_for_page(page),
                            page.title,
                            page.kind,
                            page.scope,
                            page.tags,
                            Json(page.metadata or {}),
                            now,
                            page_uuid,
                        ),
                    )
                else:
                    revision = 1
                    await cur.execute(
                        """
                        INSERT INTO astrocyte_wiki_pages
                            (page_id, bank_id, slug, title, kind, scope, tags, metadata, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            page.page_id,
                            bank_id,
                            _slug_for_page(page),
                            page.title,
                            page.kind,
                            page.scope,
                            page.tags,
                            Json(page.metadata or {}),
                            now,
                            now,
                        ),
                    )
                    page_uuid = (await cur.fetchone())["id"]

                await cur.execute(
                    """
                    INSERT INTO astrocyte_wiki_revisions
                        (page_uuid, revision_number, markdown, source_count, metadata, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        page_uuid,
                        revision,
                        page.content,
                        len(page.source_ids),
                        Json({"declared_revision": page.revision}),
                        page.revised_at or now,
                    ),
                )
                revision_id = (await cur.fetchone())["id"]

                for source_id in page.source_ids:
                    await cur.execute(
                        """
                        INSERT INTO astrocyte_wiki_revision_sources (revision_id, memory_id, bank_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (revision_id, memory_id) DO NOTHING
                        """,
                        (revision_id, source_id, bank_id),
                    )

                for link in page.cross_links:
                    await cur.execute(
                        """
                        INSERT INTO astrocyte_wiki_links (from_page_id, target_slug, link_type)
                        VALUES (%s, %s, 'related')
                        ON CONFLICT (from_page_id, target_slug, link_type) DO NOTHING
                        """,
                        (page_uuid, link),
                    )

                await cur.execute(
                    """
                    UPDATE astrocyte_wiki_pages
                    SET current_revision_id = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (revision_id, now, page_uuid),
                )
            await conn.commit()
        return page.page_id

    async def get_page(self, page_id: str, bank_id: str) -> WikiPage | None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT p.*, r.id AS revision_id, r.revision_number, r.markdown, r.created_at AS revised_at
                    FROM astrocyte_wiki_pages p
                    JOIN astrocyte_wiki_revisions r ON r.id = p.current_revision_id
                    WHERE p.bank_id = %s AND p.page_id = %s AND p.deleted_at IS NULL
                    """,
                    (bank_id, page_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                return await self._page_from_row(cur, row)

    async def list_pages(
        self,
        bank_id: str,
        scope: str | None = None,
        kind: str | None = None,
    ) -> list[WikiPage]:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        where = ["p.bank_id = %s", "p.deleted_at IS NULL"]
        params: list[Any] = [bank_id]
        if scope is not None:
            where.append("p.scope = %s")
            params.append(scope)
        if kind is not None:
            where.append("p.kind = %s")
            params.append(kind)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT p.*, r.id AS revision_id, r.revision_number, r.markdown, r.created_at AS revised_at
                    FROM astrocyte_wiki_pages p
                    JOIN astrocyte_wiki_revisions r ON r.id = p.current_revision_id
                    WHERE {" AND ".join(where)}
                    ORDER BY p.page_id
                    """,
                    params,
                )
                rows = await cur.fetchall()
                return [await self._page_from_row(cur, row) for row in rows]

    async def delete_page(self, page_id: str, bank_id: str) -> bool:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE astrocyte_wiki_pages
                    SET deleted_at = NOW()
                    WHERE bank_id = %s AND page_id = %s AND deleted_at IS NULL
                    """,
                    (bank_id, page_id),
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
            return HealthStatus(healthy=True, message="pg wiki store connected")
        except Exception as exc:
            return HealthStatus(healthy=False, message=f"pg wiki store unhealthy: {exc!s}")

    async def close(self) -> None:
        async with self._pool_lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None

    async def _page_from_row(self, cur: psycopg.AsyncCursor[dict[str, Any]], row: dict[str, Any]) -> WikiPage:
        await cur.execute(
            """
            SELECT memory_id
            FROM astrocyte_wiki_revision_sources
            WHERE revision_id = %s
            ORDER BY memory_id
            """,
            (row["revision_id"],),
        )
        sources = [source_row["memory_id"] for source_row in await cur.fetchall()]

        await cur.execute(
            """
            SELECT target_slug
            FROM astrocyte_wiki_links
            WHERE from_page_id = %s
            ORDER BY target_slug
            """,
            (row["id"],),
        )
        links = [link_row["target_slug"] for link_row in await cur.fetchall()]
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return WikiPage(
            page_id=row["page_id"],
            bank_id=row["bank_id"],
            kind=row["kind"],
            title=row["title"],
            content=row["markdown"],
            scope=row["scope"],
            source_ids=sources,
            cross_links=links,
            revision=int(row["revision_number"]),
            revised_at=row["revised_at"],
            tags=list(row["tags"]) if row["tags"] else None,
            metadata=metadata or None,
        )


def _slug_for_page(page: WikiPage) -> str:
    return page.page_id.split(":", 1)[-1] if ":" in page.page_id else page.page_id
