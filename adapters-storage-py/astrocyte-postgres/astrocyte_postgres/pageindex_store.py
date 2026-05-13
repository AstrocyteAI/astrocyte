"""PostgreSQL-backed PageIndexStore for the section recall stack (M9).

Backs the three-layer recall design described in
``docs/_design/recall.md`` — the section-grain SQL adapter that
holds the PageIndex tree (one document per conversation, sections as
tree nodes), Hindsight-style entity-mention rows, and section-to-section
links.

PR1 only exercises ``save_document`` / ``save_sections`` /
``load_document`` / ``load_skeleton`` — the minimum needed to port the
Phase A POC picker to a Postgres-backed cache. Entity / link methods are
implemented but exercised in PR2 (commits A, B, D).

Schema: ``adapters-storage-py/astrocyte-postgres/migrations/015_tier2_recall.sql``.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any, ClassVar

import psycopg
from astrocyte.tenancy import fq_table, get_current_schema
from astrocyte.types import (
    HealthStatus,
    PageIndexDocument,
    PageIndexSection,
    PageIndexSectionEntity,
    PageIndexSectionLink,
)
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_VALID_LINK_TYPES = frozenset({"semantic_knn", "causal", "supersedes", "elaborates"})


def _parse_pgvector(raw) -> list[float] | None:
    """Parse a pgvector value back to a Python list[float].

    The text protocol returns embeddings as strings like
    ``"[0.1, -0.5, ...]"``. When ``psycopg-vector`` is registered the
    adapter returns a numpy array directly; we coerce to plain list
    either way to keep this module pgvector-binding-optional.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if hasattr(raw, "tolist"):  # numpy array path (psycopg-vector)
        return [float(x) for x in raw.tolist()]
    if isinstance(raw, str):
        s = raw.strip().lstrip("[").rstrip("]")
        if not s:
            return []
        return [float(p.strip()) for p in s.split(",") if p.strip()]
    # Unknown shape — fail loudly so we notice during dev.
    raise TypeError(f"_parse_pgvector: unsupported {type(raw).__name__}")


def _embedding_param(vec: list[float] | None) -> str | None:
    """Format a Python embedding list for the pgvector text protocol.

    pgvector accepts both binary (via psycopg-vector) and text formats.
    We use text + ``::vector`` cast to keep psycopg-vector as an optional
    dep — the text format is `'[0.1, 0.2, ...]'` (literal JSON-array).
    Returns ``None`` so NULL passes through cleanly when the section
    doesn't have an embedding yet.
    """
    if vec is None:
        return None
    # Use repr for floats to avoid scientific-notation ambiguity at
    # boundary values; pgvector parses both forms but repr is unambiguous.
    return "[" + ", ".join(repr(float(x)) for x in vec) + "]"


class PostgresPageIndexStore:
    """Durable PageIndexStore using the section recall schema (migration 015).

    Per-tenant aware via :func:`astrocyte.tenancy.fq_table` — the active
    tenant schema prefixes every table name. Same connection-pool /
    schema-bootstrap shape as :class:`PostgresWikiStore`.
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
                "PostgresPageIndexStore requires `dsn` in pageindex_store_config "
                "or DATABASE_URL / ASTROCYTE_PG_DSN",
            )
        self._bootstrap_schema = bool(bootstrap_schema)
        self._pool: AsyncConnectionPool | None = None
        self._pool_lock = asyncio.Lock()
        self._bootstrapped_schemas: set[str] = set()
        self._schema_lock = asyncio.Lock()

    # ── lifecycle ───────────────────────────────────────────────────────

    def _fq(self, table: str) -> str:
        return fq_table(table)

    async def _ensure_pool(self) -> AsyncConnectionPool:
        async with self._pool_lock:
            if self._pool is None:
                async def configure(conn: psycopg.AsyncConnection) -> None:
                    # Same search_path discipline as the wiki store — pin
                    # to public so the bench DB's user-named schema doesn't
                    # silently shadow the canonical tables.
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

    async def close(self) -> None:
        """Close the connection pool. Safe to call multiple times.

        Mirrors ``PostgresStore.close()`` / ``PostgresWikiStore.close()`` /
        ``PostgresMentalModelStore.close()``. Tests and long-running
        processes should call this on shutdown so psycopg's background
        pool-manager tasks unwind cleanly — without it, asyncio-runner
        teardown can hang on dangling pool tasks (pytest-timeout fires
        after 60s on otherwise-passing tests).
        """
        async with self._pool_lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None

    async def _ensure_schema(self, pool: AsyncConnectionPool) -> None:
        """Per-tenant-aware bootstrap. Mirrors migration 015 for the
        active tenant schema. Idempotent (CREATE TABLE IF NOT EXISTS)."""
        if not self._bootstrap_schema:
            return
        active_schema = get_current_schema()
        if active_schema in self._bootstrapped_schemas:
            return
        async with self._schema_lock:
            if active_schema in self._bootstrapped_schemas:
                return
            async with pool.connection() as conn:
                # CREATE EXTENSION must run as superuser; assume the
                # operator's migrate.sh already ran. We just need the
                # tables themselves.
                async with conn.cursor() as cur:
                    await cur.execute(self._ddl_documents())
                    await cur.execute(self._ddl_sections())
                    await cur.execute(self._ddl_section_entities())
                    await cur.execute(self._ddl_section_links())
                await conn.commit()
            self._bootstrapped_schemas.add(active_schema)

    # ── DDL (mirrors 015_tier2_recall.sql for the active schema) ────────

    def _ddl_documents(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS {self._fq('astrocyte_pi_documents')} (
            id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            bank_id         TEXT         NOT NULL,
            source_id       TEXT         NOT NULL,
            md_text         TEXT         NOT NULL,
            reference_date  TIMESTAMPTZ,
            built_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (bank_id, source_id)
        );
        """

    def _ddl_sections(self) -> str:
        # PR2 commit A adds the summary_embedding column + DiskANN index.
        # Bootstrap path includes both — operators on plain pgvector who
        # can't run the diskann index should ALTER it manually.
        return f"""
        CREATE TABLE IF NOT EXISTS {self._fq('astrocyte_pi_sections')} (
            document_id        UUID         NOT NULL
                REFERENCES {self._fq('astrocyte_pi_documents')}(id) ON DELETE CASCADE,
            line_num           INT          NOT NULL,
            node_id            TEXT         NOT NULL,
            title              TEXT         NOT NULL,
            summary            TEXT,
            summary_embedding  vector(1536),
            speaker            TEXT,
            session_date       TIMESTAMPTZ,
            parent_node        TEXT,
            depth              INT          NOT NULL,
            PRIMARY KEY (document_id, line_num)
        );
        ALTER TABLE {self._fq('astrocyte_pi_sections')}
            ADD COLUMN IF NOT EXISTS summary_embedding vector(1536);
        CREATE INDEX IF NOT EXISTS ix_pi_sections_skeleton
            ON {self._fq('astrocyte_pi_sections')} (document_id, depth, line_num);
        """

    def _ddl_section_entities(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS {self._fq('astrocyte_pi_section_entities')} (
            document_id  UUID  NOT NULL,
            line_num     INT   NOT NULL,
            entity_name  TEXT  NOT NULL,
            PRIMARY KEY (document_id, line_num, entity_name),
            FOREIGN KEY (document_id, line_num)
                REFERENCES {self._fq('astrocyte_pi_sections')}(document_id, line_num) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS ix_pi_section_entities_name
            ON {self._fq('astrocyte_pi_section_entities')} (entity_name);
        """

    def _ddl_section_links(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS {self._fq('astrocyte_pi_section_links')} (
            from_doc    UUID         NOT NULL,
            from_line   INT          NOT NULL,
            to_doc      UUID         NOT NULL,
            to_line     INT          NOT NULL,
            link_type   TEXT         NOT NULL
                CHECK (link_type IN ('semantic_knn', 'causal', 'supersedes', 'elaborates')),
            weight      DOUBLE PRECISION NOT NULL,
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            PRIMARY KEY (from_doc, from_line, to_doc, to_line, link_type),
            FOREIGN KEY (from_doc, from_line)
                REFERENCES {self._fq('astrocyte_pi_sections')}(document_id, line_num) ON DELETE CASCADE,
            FOREIGN KEY (to_doc, to_line)
                REFERENCES {self._fq('astrocyte_pi_sections')}(document_id, line_num) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS ix_pi_section_links_from
            ON {self._fq('astrocyte_pi_section_links')} (from_doc, from_line, link_type);
        """

    # ── document upsert ─────────────────────────────────────────────────

    async def save_document(self, doc: PageIndexDocument) -> str:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Upsert keyed on (bank_id, source_id). Re-running tree-
                # build for the same conversation replaces the row in
                # place; the FK cascade on sections drops stale rows.
                # We let the DB generate the UUID on insert; on update,
                # we keep the existing id and bump built_at.
                await cur.execute(
                    f"""
                    INSERT INTO {self._fq('astrocyte_pi_documents')}
                        (bank_id, source_id, md_text, reference_date, built_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (bank_id, source_id) DO UPDATE SET
                        md_text = EXCLUDED.md_text,
                        reference_date = EXCLUDED.reference_date,
                        built_at = EXCLUDED.built_at
                    RETURNING id
                    """,
                    (
                        doc.bank_id,
                        doc.source_id,
                        doc.md_text,
                        doc.reference_date,
                        doc.built_at or datetime.now(tz=UTC),
                    ),
                )
                row = await cur.fetchone()
                doc_id = str(row[0])
            await conn.commit()
        return doc_id

    # ── sections bulk-replace ───────────────────────────────────────────

    async def save_sections(
        self,
        document_id: str,
        sections: list[PageIndexSection],
    ) -> int:
        if not sections:
            return 0
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        rows = [
            (
                document_id,
                s.line_num,
                s.node_id,
                s.title,
                s.summary,
                _embedding_param(s.summary_embedding),
                s.speaker,
                s.session_date,
                s.parent_node,
                s.depth,
            )
            for s in sorted(sections, key=lambda x: x.line_num)
        ]
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Atomic replace of the whole tree. The FK cascade on
                # section_entities and section_links wipes dependent
                # rows; PR2 will repopulate them.
                await cur.execute(
                    f"DELETE FROM {self._fq('astrocyte_pi_sections')} WHERE document_id = %s",
                    (document_id,),
                )
                await cur.executemany(
                    f"""
                    INSERT INTO {self._fq('astrocyte_pi_sections')}
                        (document_id, line_num, node_id, title, summary,
                         summary_embedding, speaker, session_date, parent_node, depth)
                    VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s)
                    """,
                    rows,
                )
            await conn.commit()
        return len(rows)

    async def save_section_embeddings(
        self,
        document_id: str,
        embeddings: list[tuple[int, list[float]]],
    ) -> int:
        """PR2 commit A: bulk-update ``summary_embedding`` after sections
        already exist. Used when the embedding pass runs as a separate
        post-write step (cheaper than re-issuing a full ``save_sections``
        with embeddings inlined). ``embeddings`` is an iterable of
        ``(line_num, vector)`` tuples."""
        if not embeddings:
            return 0
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        rows = [(document_id, ln, _embedding_param(v)) for ln, v in embeddings]
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    f"""
                    UPDATE {self._fq('astrocyte_pi_sections')}
                    SET summary_embedding = %s::vector
                    WHERE document_id = %s AND line_num = %s
                    """,
                    [(v, doc, ln) for doc, ln, v in rows],
                )
            await conn.commit()
        return len(rows)

    # ── reads ───────────────────────────────────────────────────────────

    async def load_document(
        self,
        bank_id: str,
        source_id: str,
    ) -> PageIndexDocument | None:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT id, bank_id, source_id, md_text,
                           reference_date, built_at
                    FROM {self._fq('astrocyte_pi_documents')}
                    WHERE bank_id = %s AND source_id = %s
                    """,
                    (bank_id, source_id),
                )
                row = await cur.fetchone()
        if row is None:
            return None
        return PageIndexDocument(
            id=str(row["id"]),
            bank_id=row["bank_id"],
            source_id=row["source_id"],
            md_text=row["md_text"],
            reference_date=row["reference_date"],
            built_at=row["built_at"],
        )

    async def load_skeleton(self, document_id: str) -> list[PageIndexSection]:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                # Project out summary_embedding (column doesn't exist in
                # PR1 anyway). Order by line_num so the picker sees the
                # tree in document order.
                await cur.execute(
                    f"""
                    SELECT document_id, line_num, node_id, title, summary,
                           speaker, session_date, parent_node, depth
                    FROM {self._fq('astrocyte_pi_sections')}
                    WHERE document_id = %s
                    ORDER BY line_num
                    """,
                    (document_id,),
                )
                rows = await cur.fetchall()
        return [
            PageIndexSection(
                document_id=str(r["document_id"]),
                line_num=r["line_num"],
                node_id=r["node_id"],
                title=r["title"],
                summary=r["summary"],
                summary_embedding=None,  # not loaded; PR2 strategy queries fetch separately
                speaker=r["speaker"],
                session_date=r["session_date"],
                parent_node=r["parent_node"],
                depth=r["depth"],
            )
            for r in rows
        ]

    # ── PR2 surfaces: entities + links (DDL ready; consumers come later) ─

    async def save_section_entities(
        self,
        entities: list[PageIndexSectionEntity],
    ) -> int:
        if not entities:
            return 0
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        rows = [(e.document_id, e.line_num, e.entity_name) for e in entities]
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # ON CONFLICT DO NOTHING — idempotent on the composite PK.
                await cur.executemany(
                    f"""
                    INSERT INTO {self._fq('astrocyte_pi_section_entities')}
                        (document_id, line_num, entity_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (document_id, line_num, entity_name) DO NOTHING
                    """,
                    rows,
                )
                # rowcount is unreliable for executemany under some psycopg
                # versions; report the input length as a best-effort count
                # of "rows attempted to write".
            await conn.commit()
        return len(rows)

    async def save_section_links(
        self,
        links: list[PageIndexSectionLink],
    ) -> int:
        if not links:
            return 0
        for link in links:
            if link.link_type not in _VALID_LINK_TYPES:
                raise ValueError(
                    f"section_links.link_type must be one of {sorted(_VALID_LINK_TYPES)!r}, "
                    f"got {link.link_type!r}"
                )
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        rows = [
            (
                link.from_doc,
                link.from_line,
                link.to_doc,
                link.to_line,
                link.link_type,
                link.weight,
            )
            for link in links
        ]
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    f"""
                    INSERT INTO {self._fq('astrocyte_pi_section_links')}
                        (from_doc, from_line, to_doc, to_line, link_type, weight)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (from_doc, from_line, to_doc, to_line, link_type) DO UPDATE SET
                        weight = EXCLUDED.weight
                    """,
                    rows,
                )
            await conn.commit()
        return len(rows)

    # ── PR2 D.7.1: semantic-kNN graph (no LLM cost) ─────────────────────

    async def populate_semantic_knn_links(
        self,
        document_id: str,
        *,
        top_k: int = 5,
        min_similarity: float = 0.5,
    ) -> int:
        """Populate ``section_links`` with ``link_type='semantic_knn'``
        for every section in this document. For each section, finds the
        ``top_k`` most-embedding-similar OTHER sections in the same
        document and inserts an edge with ``weight = 1 - cosine_distance``.

        Pure SQL — uses pgvector's ``<=>`` (cosine distance) operator
        with a ``LATERAL`` join (Hindsight's pattern). No LLM call.
        Idempotent on the composite primary key — safe to re-run.

        ``min_similarity`` filters out near-zero-similarity pairs so the
        graph isn't dominated by uninformative noise. 0.5 is a sensible
        default for ``text-embedding-3-small`` (cosine values cluster in
        ~0.3-0.9 for related text on this model).

        Why this exists: PR2-D.7's LLM-based causal/supersedes/elaborates
        extraction over-emits on LoCoMo (~110 links/doc) but
        under-emits on LME (~5 links/doc) because LME's chat-history
        shape rarely has explicit causal/correction relationships.
        Semantic-kNN restores the kind of dense topical bridging that
        the graph_expand strategy needs to lift LME multi-session.
        """
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Use RETURNING to count inserts. ON CONFLICT DO NOTHING
                # makes this idempotent — re-running just no-ops.
                # Self-loops are excluded by the line_num inequality.
                # Bidirectional links are NOT auto-created — the picker's
                # graph_expand walks both directions in its CTE, so we
                # only need each pair stored once.
                await cur.execute(
                    f"""
                    WITH inserted AS (
                        INSERT INTO {self._fq('astrocyte_pi_section_links')}
                            (from_doc, from_line, to_doc, to_line, link_type, weight)
                        SELECT s1.document_id, s1.line_num,
                               t.to_doc, t.to_line,
                               'semantic_knn',
                               t.sim
                        FROM {self._fq('astrocyte_pi_sections')} AS s1
                        CROSS JOIN LATERAL (
                            SELECT s2.document_id AS to_doc,
                                   s2.line_num   AS to_line,
                                   1 - (s1.summary_embedding <=> s2.summary_embedding) AS sim
                            FROM {self._fq('astrocyte_pi_sections')} AS s2
                            WHERE s2.document_id = s1.document_id
                              AND s2.line_num != s1.line_num
                              AND s2.summary_embedding IS NOT NULL
                            ORDER BY s1.summary_embedding <=> s2.summary_embedding
                            LIMIT %s
                        ) AS t
                        WHERE s1.document_id = %s
                          AND s1.summary_embedding IS NOT NULL
                          AND t.sim >= %s
                        ON CONFLICT (from_doc, from_line, to_doc, to_line, link_type)
                            DO NOTHING
                        RETURNING 1
                    )
                    SELECT count(*) FROM inserted
                    """,
                    (top_k, document_id, float(min_similarity)),
                )
                row = await cur.fetchone()
                inserted = int(row[0]) if row else 0
            await conn.commit()
        return inserted

    # ── PR2 commit B: parallel-strategy query methods ───────────────────
    #
    # Each method is a single SQL round-trip returning ranked
    # ``(document_id, line_num, score)`` tuples. The section recall
    # orchestrator (``astrocyte.pipeline.section_recall``) calls them in
    # parallel via asyncio.gather, then fuses via RRF.

    async def search_sections_semantic(
        self,
        bank_id: str,
        query_embedding: list[float],
        *,
        top_k: int = 20,
    ) -> list[tuple[str, int, float]]:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        embed_param = _embedding_param(query_embedding)
        if embed_param is None:
            return []
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # ``1 - (a <=> b)`` converts cosine distance → similarity
                # so RRF gets a positively-oriented score. Filter
                # NULL embeddings (sections built before PR2 commit A).
                await cur.execute(
                    f"""
                    SELECT s.document_id, s.line_num,
                           1 - (s.summary_embedding <=> %s::vector) AS score
                    FROM {self._fq('astrocyte_pi_sections')} AS s
                    JOIN {self._fq('astrocyte_pi_documents')} AS d ON d.id = s.document_id
                    WHERE d.bank_id = %s
                      AND s.summary_embedding IS NOT NULL
                    ORDER BY s.summary_embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (embed_param, bank_id, embed_param, top_k),
                )
                rows = await cur.fetchall()
        return [(str(r[0]), r[1], float(r[2])) for r in rows]

    async def search_sections_keyword(
        self,
        bank_id: str,
        query: str,
        *,
        top_k: int = 20,
        speaker: str | None = None,
        document_id: str | None = None,
    ) -> list[tuple[str, int, float]]:
        if not query.strip():
            return []
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        # Use plainto_tsquery for natural-language input; ts_rank_cd
        # gives the standard cover-density rank score.
        speaker_clause = "AND s.speaker = %s" if speaker else ""
        # PR2.6: optional single-doc scope so temporal_arithmetic.find_event_date
        # isn't starved by bank-wide top-K when 50+ docs share a bank.
        doc_clause = "AND s.document_id = %s::uuid" if document_id else ""
        params: list = [query, bank_id]
        if speaker:
            params.append(speaker)
        if document_id:
            params.append(document_id)
        params.extend([query, top_k])
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
                    SELECT s.document_id, s.line_num,
                           ts_rank_cd(
                               to_tsvector('english',
                                   coalesce(s.title, '') || ' ' || coalesce(s.summary, '')
                               ),
                               (SELECT tsq FROM q)
                           ) AS score
                    FROM {self._fq('astrocyte_pi_sections')} AS s
                    JOIN {self._fq('astrocyte_pi_documents')} AS d ON d.id = s.document_id
                    WHERE d.bank_id = %s
                      {speaker_clause}
                      {doc_clause}
                      AND to_tsvector('english',
                              coalesce(s.title, '') || ' ' || coalesce(s.summary, '')
                          ) @@ (SELECT tsq FROM q)
                    ORDER BY ts_rank_cd(
                        to_tsvector('english',
                            coalesce(s.title, '') || ' ' || coalesce(s.summary, '')
                        ),
                        plainto_tsquery('english', %s)
                    ) DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = await cur.fetchall()
        return [(str(r[0]), r[1], float(r[2])) for r in rows]

    async def search_sections_by_entities(
        self,
        bank_id: str,
        entity_names: list[str],
        *,
        top_k: int = 20,
    ) -> list[tuple[str, int, float]]:
        if not entity_names:
            return []
        # Case-insensitive match — entity extraction stores the canonical
        # form as written ("Caroline"); the question may use any case.
        normalized = [n.strip() for n in entity_names if n and n.strip()]
        if not normalized:
            return []
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Hindsight's CTE pattern at section grain: count distinct
                # matching entities per section, rank by count.
                await cur.execute(
                    f"""
                    SELECT se.document_id, se.line_num,
                           COUNT(DISTINCT lower(se.entity_name))::float AS score
                    FROM {self._fq('astrocyte_pi_section_entities')} AS se
                    JOIN {self._fq('astrocyte_pi_documents')} AS d ON d.id = se.document_id
                    WHERE d.bank_id = %s
                      AND lower(se.entity_name) = ANY(%s::text[])
                    GROUP BY se.document_id, se.line_num
                    ORDER BY score DESC
                    LIMIT %s
                    """,
                    (bank_id, [n.lower() for n in normalized], top_k),
                )
                rows = await cur.fetchall()
        return [(str(r[0]), r[1], float(r[2])) for r in rows]

    async def search_sections_temporal(
        self,
        bank_id: str,
        date_range,  # tuple[datetime, datetime]
        *,
        top_k: int = 20,
    ) -> list[tuple[str, int, float]]:
        start, end = date_range
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Uses ix_pi_sections_date partial index from migration
                # 015. Score is uniform 1.0 — temporal is a filter, not
                # a ranker. RRF combines with other strategies' scores.
                await cur.execute(
                    f"""
                    SELECT s.document_id, s.line_num, 1.0 AS score
                    FROM {self._fq('astrocyte_pi_sections')} AS s
                    JOIN {self._fq('astrocyte_pi_documents')} AS d ON d.id = s.document_id
                    WHERE d.bank_id = %s
                      AND s.session_date IS NOT NULL
                      AND s.session_date BETWEEN %s AND %s
                    ORDER BY s.session_date
                    LIMIT %s
                    """,
                    (bank_id, start, end, top_k),
                )
                rows = await cur.fetchall()
        return [(str(r[0]), r[1], float(r[2])) for r in rows]

    async def expand_section_links(
        self,
        seeds: list[tuple[str, int]],
        *,
        link_types: list[str] | None = None,
        top_k: int = 20,
    ) -> list[tuple[str, int, float]]:
        if not seeds:
            return []
        # Pass seeds as two parallel arrays (docs + lines) so we can
        # ``unnest`` them with a clean join. Postgres doesn't have a
        # nice composite-pair literal across psycopg versions.
        seed_docs = [d for d, _ in seeds]
        seed_lines = [ln for _, ln in seeds]
        link_clause = ""
        # Build params: seeds (×2 for the two unnest sides), then
        # link_types twice (once per UNION arm) if filtering, then top_k.
        link_params: list = []
        if link_types:
            link_clause = "AND sl.link_type = ANY(%s::text[])"
            link_params = [link_types, link_types]
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    WITH seeds AS (
                        SELECT seed_doc::uuid AS doc, seed_line::int AS line
                        FROM unnest(%s::uuid[], %s::int[])
                            AS t(seed_doc, seed_line)
                    )
                    SELECT edges.to_doc::text, edges.to_line, SUM(edges.weight)::float AS score
                    FROM (
                        SELECT sl.to_doc, sl.to_line, sl.weight
                        FROM {self._fq('astrocyte_pi_section_links')} AS sl
                        JOIN seeds ON sl.from_doc = seeds.doc AND sl.from_line = seeds.line
                        WHERE 1=1 {link_clause}
                        UNION ALL
                        SELECT sl.from_doc AS to_doc, sl.from_line AS to_line, sl.weight
                        FROM {self._fq('astrocyte_pi_section_links')} AS sl
                        JOIN seeds ON sl.to_doc = seeds.doc AND sl.to_line = seeds.line
                        WHERE 1=1 {link_clause}
                    ) AS edges
                    GROUP BY edges.to_doc, edges.to_line
                    ORDER BY score DESC
                    LIMIT %s
                    """,
                    tuple([seed_docs, seed_lines] + link_params + [top_k]),
                )
                rows = await cur.fetchall()
        return [(str(r[0]), r[1], float(r[2])) for r in rows]

    async def list_distinct_entities(
        self,
        bank_id: str,
        document_id: str,
        *,
        pattern: str | None = None,
        limit: int = 50,
    ) -> list[tuple[str, int]]:
        # bank_id is unused at the SQL layer because the entity rows
        # are already scoped to the document_id (which itself belongs
        # to one bank). Kept in the signature to match the SPI shape.
        del bank_id
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        if pattern is not None:
            sql = f"""
                SELECT entity_name, COUNT(*) AS mentions
                FROM {self._fq('astrocyte_pi_section_entities')}
                WHERE document_id = %s AND entity_name ILIKE %s
                GROUP BY entity_name
                ORDER BY mentions DESC, entity_name ASC
                LIMIT %s
            """
            # If caller didn't include % wildcards, treat as substring.
            ilike = pattern if "%" in pattern else f"%{pattern}%"
            params = (document_id, ilike, max(1, limit))
        else:
            sql = f"""
                SELECT entity_name, COUNT(*) AS mentions
                FROM {self._fq('astrocyte_pi_section_entities')}
                WHERE document_id = %s
                GROUP BY entity_name
                ORDER BY mentions DESC, entity_name ASC
                LIMIT %s
            """
            params = (document_id, max(1, limit))
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        return [(str(r[0]), int(r[1])) for r in rows]

    # ── M12.1 fact-grain ───────────────────────────────────────────

    async def save_facts(self, facts) -> int:
        if not facts:
            return 0
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        rows = []
        for f in facts:
            rows.append((
                f.id, f.bank_id, f.document_id, f.line_num,
                f.text, f.fact_type, f.speaker,
                f.occurred_start, f.occurred_end,
                list(f.entities or []),
                _embedding_param(f.embedding) if f.embedding else None,
            ))
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    f"""
                    INSERT INTO {self._fq('astrocyte_pi_facts')}
                        (id, bank_id, document_id, line_num,
                         fact_text, fact_type, speaker,
                         occurred_start, occurred_end, entities, embedding)
                    VALUES (%s, %s, %s::uuid, %s,
                            %s, %s, %s,
                            %s, %s, %s, %s::vector)
                    """,
                    rows,
                )
        return len(rows)

    async def update_fact_embeddings(self, embeddings) -> int:
        if not embeddings:
            return 0
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        rows = [(_embedding_param(emb), fid) for fid, emb in embeddings if emb]
        if not rows:
            return 0
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    f"""
                    UPDATE {self._fq('astrocyte_pi_facts')}
                    SET embedding = %s::vector
                    WHERE id = %s::uuid
                    """,
                    rows,
                )
        return len(rows)

    async def search_facts_semantic(
        self,
        bank_id: str,
        query_embedding: list[float],
        *,
        top_k: int = 20,
        document_id: str | None = None,
        fact_type: str | None = None,
    ):
        from astrocyte.types import PageIndexFactHit
        if not query_embedding:
            return []
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        params: list = [_embedding_param(query_embedding), bank_id]
        doc_clause = ""
        if document_id:
            doc_clause = "AND document_id = %s::uuid"
            params.append(document_id)
        type_clause = ""
        if fact_type:
            type_clause = "AND fact_type = %s"
            params.append(fact_type)
        params.extend([_embedding_param(query_embedding), top_k])
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT id, document_id, line_num, fact_text, fact_type,
                           speaker, occurred_start, occurred_end, entities,
                           1 - (embedding <=> %s::vector) AS score
                    FROM {self._fq('astrocyte_pi_facts')}
                    WHERE bank_id = %s
                      AND embedding IS NOT NULL
                      {doc_clause}
                      {type_clause}
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = await cur.fetchall()
        return [
            PageIndexFactHit(
                fact_id=str(r[0]), document_id=str(r[1]), line_num=r[2],
                text=r[3], fact_type=r[4], speaker=r[5],
                occurred_start=r[6], occurred_end=r[7],
                entities=list(r[8] or []), score=float(r[9]),
            )
            for r in rows
        ]

    async def search_facts_by_entity(
        self,
        bank_id: str,
        entity_name: str,
        *,
        top_k: int = 50,
        document_id: str | None = None,
    ):
        from astrocyte.types import PageIndexFactHit
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        params: list = [bank_id, [entity_name]]
        doc_clause = ""
        if document_id:
            doc_clause = "AND document_id = %s::uuid"
            params.append(document_id)
        params.append(top_k)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT id, document_id, line_num, fact_text, fact_type,
                           speaker, occurred_start, occurred_end, entities
                    FROM {self._fq('astrocyte_pi_facts')}
                    WHERE bank_id = %s
                      AND entities && %s::text[]
                      {doc_clause}
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = await cur.fetchall()
        return [
            PageIndexFactHit(
                fact_id=str(r[0]), document_id=str(r[1]), line_num=r[2],
                text=r[3], fact_type=r[4], speaker=r[5],
                occurred_start=r[6], occurred_end=r[7],
                entities=list(r[8] or []), score=1.0,
            )
            for r in rows
        ]

    async def search_facts_temporal(
        self,
        bank_id: str,
        date_range,
        *,
        top_k: int = 50,
        document_id: str | None = None,
    ):
        from astrocyte.types import PageIndexFactHit
        start, end = date_range
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        params: list = [bank_id, start, end]
        doc_clause = ""
        if document_id:
            doc_clause = "AND document_id = %s::uuid"
            params.append(document_id)
        params.append(top_k)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT id, document_id, line_num, fact_text, fact_type,
                           speaker, occurred_start, occurred_end, entities
                    FROM {self._fq('astrocyte_pi_facts')}
                    WHERE bank_id = %s
                      AND occurred_start IS NOT NULL
                      AND occurred_start BETWEEN %s AND %s
                      {doc_clause}
                    ORDER BY occurred_start
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = await cur.fetchall()
        return [
            PageIndexFactHit(
                fact_id=str(r[0]), document_id=str(r[1]), line_num=r[2],
                text=r[3], fact_type=r[4], speaker=r[5],
                occurred_start=r[6], occurred_end=r[7],
                entities=list(r[8] or []), score=1.0,
            )
            for r in rows
        ]

    async def count_facts_matching(
        self,
        bank_id: str,
        document_id: str,
        *,
        entity_pattern: str | None = None,
        fact_type: str | None = None,
    ) -> int:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        params: list = [bank_id, document_id]
        clauses = []
        if entity_pattern is not None:
            # ILIKE-match any element of the entities array.
            clauses.append(
                "EXISTS (SELECT 1 FROM unnest(entities) AS e WHERE e ILIKE %s)"
            )
            ilike = entity_pattern if "%" in entity_pattern else f"%{entity_pattern}%"
            params.append(ilike)
        if fact_type is not None:
            clauses.append("fact_type = %s")
            params.append(fact_type)
        where_extra = (" AND " + " AND ".join(clauses)) if clauses else ""
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {self._fq('astrocyte_pi_facts')}
                    WHERE bank_id = %s AND document_id = %s::uuid
                    {where_extra}
                    """,
                    tuple(params),
                )
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def save_section_event_dates(
        self,
        document_id: str,
        event_dates,
    ) -> int:
        if not event_dates:
            return 0
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                rows = [
                    (start, end, document_id, line_num)
                    for line_num, start, end in event_dates
                ]
                await cur.executemany(
                    f"""
                    UPDATE {self._fq('astrocyte_pi_sections')}
                    SET occurred_start = %s, occurred_end = %s
                    WHERE document_id = %s AND line_num = %s
                    """,
                    rows,
                )
                # psycopg returns -1 for executemany; assume all rows
                # matched (caller scoped them to known sections).
                return len(rows)

    # ── M10.1 wiki / consolidation ──────────────────────────────────────

    async def load_sections_with_embeddings(
        self,
        bank_id: str,
        document_id: str,
    ):
        from astrocyte.types import PageIndexSection
        del bank_id  # documents are bank-scoped via pi_documents.bank_id
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT line_num, node_id, title, summary,
                           summary_embedding, speaker, session_date,
                           parent_node, depth, occurred_start, occurred_end
                    FROM {self._fq('astrocyte_pi_sections')}
                    WHERE document_id = %s
                    ORDER BY line_num
                    """,
                    (document_id,),
                )
                rows = await cur.fetchall()
        out: list[PageIndexSection] = []
        for r in rows:
            emb = _parse_pgvector(r[4])
            out.append(PageIndexSection(
                document_id=document_id,
                line_num=r[0],
                node_id=r[1] or "",
                title=r[2] or "",
                summary=r[3],
                summary_embedding=emb,
                speaker=r[5],
                session_date=r[6],
                parent_node=r[7],
                depth=r[8] or 0,
                occurred_start=r[9],
                occurred_end=r[10],
            ))
        return out

    async def save_wiki_page(
        self,
        *,
        page,
        embedding: list[float] | None,
        provenance: list[tuple[str, int]],
    ) -> str:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    # Upsert the page row (keyed on bank_id + page_id per
                    # migration 007's UNIQUE constraint). On conflict, bump
                    # the existing row's updated_at.
                    await cur.execute(
                        f"""
                        INSERT INTO {self._fq('astrocyte_wiki_pages')}
                            (page_id, bank_id, slug, title, kind, scope,
                             confidence, tags, metadata, current_embedding)
                        VALUES (%s, %s, %s, %s, %s, %s, 1.0, NULL, '{{}}'::jsonb, %s)
                        ON CONFLICT (bank_id, page_id) DO UPDATE SET
                            title = EXCLUDED.title,
                            current_embedding = EXCLUDED.current_embedding,
                            updated_at = NOW()
                        RETURNING id
                        """,
                        (
                            page.page_id, page.bank_id,
                            page.page_id,  # slug == page_id (unique enough)
                            page.title, page.kind, page.scope,
                            embedding,
                        ),
                    )
                    row = await cur.fetchone()
                    page_uuid = row[0]
                    # Insert a revision row with the markdown content.
                    await cur.execute(
                        f"""
                        INSERT INTO {self._fq('astrocyte_wiki_revisions')}
                            (page_uuid, revision_number, markdown,
                             summary, compiled_by, source_count, tokens_used)
                        VALUES (%s, %s, %s, %s, 'section_compile', %s, 0)
                        ON CONFLICT (page_uuid, revision_number) DO UPDATE SET
                            markdown = EXCLUDED.markdown
                        RETURNING id
                        """,
                        (
                            page_uuid, page.revision, page.content,
                            page.title, len(page.source_ids),
                        ),
                    )
                    rev_row = await cur.fetchone()
                    rev_id = rev_row[0]
                    await cur.execute(
                        f"""
                        UPDATE {self._fq('astrocyte_wiki_pages')}
                        SET current_revision_id = %s
                        WHERE id = %s
                        """,
                        (rev_id, page_uuid),
                    )
                    # Replace provenance rows for this page.
                    await cur.execute(
                        f"""
                        DELETE FROM {self._fq('astrocyte_pi_wiki_provenance')}
                        WHERE wiki_page_id = %s
                        """,
                        (page_uuid,),
                    )
                    if provenance:
                        await cur.executemany(
                            f"""
                            INSERT INTO {self._fq('astrocyte_pi_wiki_provenance')}
                                (wiki_page_id, document_id, line_num)
                            VALUES (%s, %s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            [
                                (page_uuid, doc_id, line_num)
                                for doc_id, line_num in provenance
                            ],
                        )
        return page.page_id

    async def search_wiki_pages_semantic(
        self,
        bank_id: str,
        query_embedding: list[float],
        *,
        top_k: int = 5,
        document_id: str | None = None,
    ):
        from astrocyte.types import WikiPageHit
        if not query_embedding:
            return []
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        scope_clause = "AND p.scope = %s" if document_id is not None else ""
        params: list = [query_embedding, bank_id]
        if document_id is not None:
            params.append(f"document:{document_id}")
        params.extend([query_embedding, top_k])
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT p.page_id, p.title, r.markdown, p.scope, p.kind,
                           1 - (p.current_embedding <=> %s::vector) AS score,
                           p.bank_id
                    FROM {self._fq('astrocyte_wiki_pages')} AS p
                    LEFT JOIN {self._fq('astrocyte_wiki_revisions')} AS r
                        ON r.id = p.current_revision_id
                    WHERE p.bank_id = %s
                      AND p.deleted_at IS NULL
                      AND p.current_embedding IS NOT NULL
                      {scope_clause}
                    ORDER BY p.current_embedding <=> %s::vector
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = await cur.fetchall()
        out: list[WikiPageHit] = []
        for r in rows:
            out.append(WikiPageHit(
                page_id=r[0],
                title=r[1] or "",
                content=r[2] or "",
                scope=r[3] or "",
                kind=r[4] or "topic",
                score=float(r[5]),
                source_ids=[],  # provenance lookup is a separate query
                bank_id=r[6] or bank_id,
            ))
        return out

    async def count_wiki_pages_for_doc(
        self,
        bank_id: str,
        document_id: str,
    ) -> int:
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {self._fq('astrocyte_wiki_pages')}
                    WHERE bank_id = %s
                      AND scope = %s
                      AND deleted_at IS NULL
                    """,
                    (bank_id, f"document:{document_id}"),
                )
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list_wiki_pages_for_doc(
        self,
        bank_id: str,
        document_id: str,
    ):
        """M12.6: enumerate current-revision wiki pages for one document.

        Returns WikiPage rows joined with their current revision's
        markdown content. Used by the revision pass to revise existing
        pages against their provenance sections in chronological order.
        """
        from datetime import datetime, timezone

        from astrocyte.types import WikiPage
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT p.page_id, p.title, p.kind, p.scope,
                           r.markdown, r.revision_number, r.created_at,
                           p.bank_id
                    FROM {self._fq('astrocyte_wiki_pages')} AS p
                    LEFT JOIN {self._fq('astrocyte_wiki_revisions')} AS r
                        ON r.id = p.current_revision_id
                    WHERE p.bank_id = %s
                      AND p.scope = %s
                      AND p.deleted_at IS NULL
                    ORDER BY p.page_id
                    """,
                    (bank_id, f"document:{document_id}"),
                )
                rows = await cur.fetchall()
                # Pull provenance for these pages in one query.
                page_ids = [r[0] for r in rows]
                provenance_by_page: dict[str, list[str]] = {}
                if page_ids:
                    await cur.execute(
                        f"""
                        SELECT p.page_id, prov.document_id, prov.line_num
                        FROM {self._fq('astrocyte_wiki_pages')} AS p
                        JOIN {self._fq('astrocyte_pi_wiki_provenance')} AS prov
                            ON prov.wiki_page_id = p.id
                        WHERE p.bank_id = %s
                          AND p.page_id = ANY(%s)
                        """,
                        (bank_id, page_ids),
                    )
                    for prov_row in await cur.fetchall():
                        provenance_by_page.setdefault(prov_row[0], []).append(
                            f"{prov_row[1]}:{prov_row[2]}",
                        )

        pages: list[WikiPage] = []
        for r in rows:
            revised_at = r[6] if r[6] is not None else datetime.now(tz=timezone.utc)
            pages.append(WikiPage(
                page_id=r[0],
                bank_id=r[7] or bank_id,
                kind=r[2] or "topic",
                title=r[1] or "",
                content=r[4] or "",
                scope=r[3] or "",
                source_ids=provenance_by_page.get(r[0], []),
                cross_links=[],
                revision=int(r[5]) if r[5] is not None else 1,
                revised_at=revised_at,
                tags=None,
                metadata=None,
            ))
        return pages

    async def list_wikis_affected_by_entities(
        self,
        bank_id: str,
        entities: list[str],
        *,
        min_overlap: int = 1,
        limit: int = 8,
    ):
        """M14.2: SQL entity-overlap JOIN over wiki provenance.

        Cheap query — both legs are indexed:
        - ``astrocyte_pi_wiki_provenance`` has ``ix_pi_wiki_provenance_section``
          on ``(document_id, line_num)``.
        - ``astrocyte_pi_section_entities`` (M9 PR2) carries the
          per-section entity index used by ``list_distinct_entities``.

        Returns the current-revision wikis for ``bank_id`` whose
        provenance sections share at least ``min_overlap`` entities
        with the input set. Joins back to ``astrocyte_wiki_revisions``
        for ``markdown`` + ``revision_number`` so callers receive a
        fully-hydrated ``WikiPage`` per row.

        Sorted descending by overlap count then ascending by
        ``page_id`` for stability.
        """
        if not entities:
            return []
        from datetime import datetime, timezone

        from astrocyte.types import WikiPage
        pool = await self._ensure_pool()
        await self._ensure_schema(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT p.page_id,
                           p.title, p.kind, p.scope,
                           r.markdown, r.revision_number, r.created_at,
                           p.bank_id,
                           COUNT(DISTINCT se.entity_name) AS overlap_count,
                           array_agg(DISTINCT se.entity_name
                                     ORDER BY se.entity_name) AS shared_entities
                    FROM {self._fq('astrocyte_wiki_pages')} AS p
                    JOIN {self._fq('astrocyte_pi_wiki_provenance')} AS prov
                        ON prov.wiki_page_id = p.id
                    JOIN {self._fq('astrocyte_pi_section_entities')} AS se
                        ON se.document_id = prov.document_id
                       AND se.line_num    = prov.line_num
                    LEFT JOIN {self._fq('astrocyte_wiki_revisions')} AS r
                        ON r.id = p.current_revision_id
                    WHERE p.bank_id = %s
                      AND p.deleted_at IS NULL
                      AND se.entity_name = ANY(%s)
                    GROUP BY p.page_id, p.title, p.kind, p.scope,
                             r.markdown, r.revision_number, r.created_at, p.bank_id
                    HAVING COUNT(DISTINCT se.entity_name) >= %s
                    ORDER BY overlap_count DESC, p.page_id ASC
                    LIMIT %s
                    """,
                    (bank_id, list(entities), min_overlap, limit),
                )
                rows = await cur.fetchall()

        results: list[tuple[WikiPage, int, list[str]]] = []
        for r in rows:
            revised_at = r[6] if r[6] is not None else datetime.now(tz=timezone.utc)
            page = WikiPage(
                page_id=r[0],
                bank_id=r[7] or bank_id,
                kind=r[2] or "topic",
                title=r[1] or "",
                content=r[4] or "",
                scope=r[3] or "",
                source_ids=[],
                cross_links=[],
                revision=int(r[5]) if r[5] is not None else 1,
                revised_at=revised_at,
                tags=None,
                metadata=None,
            )
            results.append((page, int(r[8]), list(r[9])))
        return results

    # ── health ──────────────────────────────────────────────────────────

    async def health(self) -> HealthStatus:
        try:
            pool = await self._ensure_pool()
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
            return HealthStatus(healthy=True, message="postgres pageindex store")
        except Exception as exc:  # noqa: BLE001 — health check
            return HealthStatus(healthy=False, message=f"{type(exc).__name__}: {exc!s}"[:200])
