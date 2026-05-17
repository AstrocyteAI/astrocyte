"""PostgresDocumentStore — Postgres impl of astrocyte.documents.DocumentStore.

Lives in the storage adapter package (not in astrocyte/) so the
Document Engine doesn't depend on Postgres directly. Application code
imports from ``astrocyte.documents`` for the SPI + InMemory impl;
production callers construct ``PostgresDocumentStore`` when they want
durable storage.

Schema lives in migrations 025 (documents) + 026 (document_nodes).
``bootstrap_schema=True`` (default) creates the tables on first use,
mirroring the pattern used by ``PostgresPageIndexStore``.

Composability invariant (per docs/_design/m17-pageindex-ingestion.md
§3.2): this store does NOT reference any Memory Engine table. Adding a
FK from astrocyte_pi_facts.tree_node_id would tightly couple the two
engines and break standalone use of either; we deliberately don't do it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import psycopg
from astrocyte.documents.storage import DocumentStore
from astrocyte.documents.types import (
    Document,
    DocumentTree,
    NodeSummary,
    TreeNode,
)
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


class PostgresDocumentStore(DocumentStore):
    """Durable DocumentStore on Postgres (migrations 025 + 026)."""

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
                "PostgresDocumentStore requires `dsn` or DATABASE_URL / ASTROCYTE_PG_DSN",
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
                    await cur.execute(_DDL_DOCUMENTS)
                    await cur.execute(_DDL_DOCUMENT_NODES)
                    await cur.execute(_INDEXES)
                await conn.commit()
            self._schema_ready = True

    # ── save ──────────────────────────────────────────────────────────

    async def save_document(
        self,
        document: Document,
        tree: DocumentTree | None = None,
    ) -> None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Upsert the document row
                await cur.execute(
                    """
                    INSERT INTO astrocyte_documents
                        (id, source_uri, content_hash, mime_type, title, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        source_uri   = EXCLUDED.source_uri,
                        content_hash = EXCLUDED.content_hash,
                        mime_type    = EXCLUDED.mime_type,
                        title        = EXCLUDED.title
                    """,
                    (
                        document.id,
                        document.source_uri,
                        document.content_hash,
                        document.mime_type,
                        document.title,
                        document.created_at,
                    ),
                )
                if tree is not None:
                    # Replace any prior tree: cascade delete via FK
                    await cur.execute(
                        "DELETE FROM astrocyte_document_nodes WHERE document_id = %s",
                        (document.id,),
                    )
                    # Insert nodes in pre-order (parents before children) so the
                    # parent_id FK is always satisfied at insert time.
                    nodes_in_order = tree.all_nodes()
                    if nodes_in_order:
                        # Reassign document_id on every node to the canonical
                        # one (defensive; tree.document_id should already match).
                        rows = [
                            (
                                n.id,
                                document.id,
                                n.parent_id,
                                n.depth,
                                n.title,
                                n.text,
                                n.summary.text if n.summary else None,
                                n.summary.kind if n.summary else None,
                                n.summary.token_count if n.summary else None,
                                n.line_start,
                                n.line_end,
                            )
                            for n in nodes_in_order
                        ]
                        await cur.executemany(
                            """
                            INSERT INTO astrocyte_document_nodes
                                (id, document_id, parent_id, depth, title, text,
                                 summary_text, summary_kind, summary_tokens,
                                 line_start, line_end)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            rows,
                        )
            await conn.commit()

    # ── reads ─────────────────────────────────────────────────────────

    async def get_document(self, document_id: str) -> Document | None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, source_uri, content_hash, mime_type, title, created_at
                    FROM astrocyte_documents
                    WHERE id = %s
                    """,
                    (document_id,),
                )
                row = await cur.fetchone()
        if row is None:
            return None
        return Document(
            id=str(row[0]),
            source_uri=row[1] or "",
            content_hash=row[2] or "",
            mime_type=row[3] or "text/markdown",
            title=row[4] or "",
            created_at=row[5],
            tree=None,
        )

    async def get_tree(self, document_id: str) -> DocumentTree | None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, parent_id, depth, title, text,
                           summary_text, summary_kind, summary_tokens,
                           line_start, line_end
                    FROM astrocyte_document_nodes
                    WHERE document_id = %s
                    ORDER BY line_start NULLS LAST, depth
                    """,
                    (document_id,),
                )
                rows = await cur.fetchall()
        if not rows:
            # Distinguish "no tree exists" from "tree exists but is empty".
            # Check whether the document itself exists first.
            doc = await self.get_document(document_id)
            return DocumentTree(document_id=document_id, roots=[]) if doc else None

        return _rebuild_tree(document_id, rows)

    async def list_documents(self, *, limit: int = 100) -> list[Document]:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, source_uri, content_hash, mime_type, title, created_at
                    FROM astrocyte_documents
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = await cur.fetchall()
        return [
            Document(
                id=str(r[0]),
                source_uri=r[1] or "",
                content_hash=r[2] or "",
                mime_type=r[3] or "text/markdown",
                title=r[4] or "",
                created_at=r[5],
                tree=None,
            )
            for r in rows
        ]

    # ── delete ────────────────────────────────────────────────────────

    async def delete_document(self, document_id: str) -> None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        # FK CASCADE on astrocyte_document_nodes drops the tree rows automatically
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM astrocyte_documents WHERE id = %s",
                    (document_id,),
                )
            await conn.commit()


# ─── tree rebuild ─────────────────────────────────────────────────────


def _rebuild_tree(document_id: str, rows: list[Any]) -> DocumentTree:
    """Reconstruct a DocumentTree from flat node rows.

    Rows are tuples in column order matching ``get_tree``'s SELECT.
    Roots are nodes with parent_id IS NULL. Children attach via
    parent_id FK lookup. Stable child order: insertion order of the
    SQL result set (line_start ASC by default).
    """
    nodes_by_id: dict[str, TreeNode] = {}
    parent_to_children: dict[str | None, list[TreeNode]] = {}
    for r in rows:
        node = TreeNode(
            id=str(r[0]),
            parent_id=str(r[1]) if r[1] is not None else None,
            depth=int(r[2]),
            title=r[3] or "",
            text=r[4] or "",
            summary=_summary_from_row(r[5], r[6], r[7]),
            children=[],
            line_start=r[8],
            line_end=r[9],
        )
        nodes_by_id[node.id] = node
        parent_to_children.setdefault(node.parent_id, []).append(node)

    # Wire children
    for parent_id, kids in parent_to_children.items():
        if parent_id is None:
            continue
        parent = nodes_by_id.get(parent_id)
        if parent is None:
            logger.warning(
                "_rebuild_tree: orphan node parent_id=%s not found for doc=%s",
                parent_id,
                document_id,
            )
            continue
        parent.children = kids

    roots = parent_to_children.get(None, [])
    return DocumentTree(document_id=document_id, roots=roots)


def _summary_from_row(text: str | None, kind: str | None, tokens: int | None) -> NodeSummary | None:
    if text is None:
        return None
    valid_kind = kind if kind in ("raw", "llm", "prefix") else "raw"
    return NodeSummary(text=text, kind=valid_kind, token_count=tokens)  # type: ignore[arg-type]


# ─── inline DDL used by _ensure_schema (mirrors migrations 025+026) ───

_DDL_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS astrocyte_documents (
    id              UUID PRIMARY KEY,
    source_uri      TEXT NOT NULL DEFAULT '',
    content_hash    TEXT NOT NULL DEFAULT '',
    mime_type       TEXT NOT NULL DEFAULT 'text/markdown',
    title           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_DDL_DOCUMENT_NODES = """
CREATE TABLE IF NOT EXISTS astrocyte_document_nodes (
    id               UUID PRIMARY KEY,
    document_id      UUID NOT NULL REFERENCES astrocyte_documents(id) ON DELETE CASCADE,
    parent_id        UUID REFERENCES astrocyte_document_nodes(id) ON DELETE CASCADE,
    depth            SMALLINT NOT NULL CHECK (depth BETWEEN 1 AND 6),
    title            TEXT NOT NULL DEFAULT '',
    text             TEXT NOT NULL DEFAULT '',
    summary_text     TEXT,
    summary_kind     TEXT CHECK (summary_kind IN ('raw', 'llm', 'prefix')),
    summary_tokens   INTEGER,
    line_start       INTEGER,
    line_end         INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS astrocyte_documents_content_hash_idx
    ON astrocyte_documents (content_hash)
    WHERE content_hash <> '';
CREATE INDEX IF NOT EXISTS astrocyte_documents_created_at_idx
    ON astrocyte_documents (created_at DESC);
CREATE INDEX IF NOT EXISTS astrocyte_document_nodes_doc_line_idx
    ON astrocyte_document_nodes (document_id, line_start);
CREATE INDEX IF NOT EXISTS astrocyte_document_nodes_parent_idx
    ON astrocyte_document_nodes (parent_id)
    WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS astrocyte_document_nodes_roots_idx
    ON astrocyte_document_nodes (document_id)
    WHERE parent_id IS NULL;
CREATE INDEX IF NOT EXISTS astrocyte_document_nodes_doc_depth_idx
    ON astrocyte_document_nodes (document_id, depth);
"""
