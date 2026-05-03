"""Apache AGE GraphStore adapter for Astrocyte.

Uses PostgreSQL with the Apache AGE extension to store entities and
relationships as a property graph.  Memory-to-entity associations are kept
in a plain PostgreSQL table (``astrocyte_age_mem_entity``) so that memory
IDs from the VectorStore can be cross-referenced without a graph traversal.

AGE Cypher is executed via the ``ag_catalog.cypher()`` SQL function.
All internal values passed into Cypher strings are sanitised through
``_q()`` before interpolation — they originate from controlled code paths
(entity IDs, bank IDs, names extracted by the LLM pipeline), not raw user
input, so the risk surface is minimal.

Prerequisites
-------------
- PostgreSQL 16 with the AGE extension installed (see
  ``docker/astrocyte-postgres/Dockerfile``).
- Run migrations in ``migrations/`` order before first use, or set
  ``bootstrap_schema=True`` (default) to auto-create on first connection.
- The database user needs ``SUPERUSER`` or ``pg_read_server_files`` to run
  ``LOAD 'age'``; or pre-load AGE in ``shared_preload_libraries``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import Any, ClassVar

import psycopg
from astrocyte.types import (
    Entity,
    EntityCandidateMatch,
    EntityLink,
    GraphHit,
    HealthStatus,
    MemoryEntityAssociation,
)
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

_logger = logging.getLogger("astrocyte.age")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IDENT_SAFE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Used by ``find_entity_candidates_scored`` to compute embedding-tier
    similarity in Python after fetching the candidate's embedding column
    as a text literal. Returns ``0.0`` for zero-vectors or mismatched
    lengths rather than raising.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _safe_graph_name(name: str) -> str:
    if not _IDENT_SAFE.match(name):
        raise ValueError(f"Invalid AGE graph name: {name!r}")
    return name


def _q(value: str) -> str:
    """Escape a string value for inline embedding inside a Cypher literal.

    Replaces backslashes then single quotes so the result is safe to wrap
    in Cypher single-quote string delimiters.  All callers pass internally-
    generated values (IDs, names, bank_ids) — not raw user strings.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _parse_agtype(raw: Any) -> dict[str, Any]:
    """Parse an AGE agtype value into a Python dict.

    AGE returns vertices/edges as strings like:
        {"id": 844424930131969, "label": "Entity",
         "properties": {"id": "alice", ...}}::vertex

    Scalar strings come back as Python str/int/float already handled by
    the caller.  This function handles the vertex/edge object form.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    # Strip ::vertex / ::edge type suffix
    stripped = re.sub(r"::(vertex|edge|path)$", "", raw.strip())
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return {}
    if isinstance(data, dict) and "properties" in data:
        return dict(data["properties"])
    return data if isinstance(data, dict) else {}


def _parse_scalar(raw: Any) -> str:
    """Extract a scalar string from an agtype value."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        # Quoted agtype strings arrive as '"value"' (JSON-encoded)
        stripped = raw.strip()
        if stripped.startswith('"') and stripped.endswith('"'):
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                pass
        return stripped
    return str(raw)


# ---------------------------------------------------------------------------
# AgeGraphStore
# ---------------------------------------------------------------------------


class AgeGraphStore:
    """GraphStore backed by Apache AGE on PostgreSQL.

    All entity/relationship data lives in an AGE property graph;
    memory-entity links are stored in a regular PostgreSQL table.

    Args:
        dsn: PostgreSQL connection string.  Falls back to ``DATABASE_URL``
            or ``ASTROCYTE_AGE_DSN`` environment variables.
        graph_name: AGE graph name (must be a valid PostgreSQL identifier).
            Defaults to ``"astrocyte"``.
        bootstrap_schema: When ``True`` (default), creates the AGE graph
            and the memory-entity mapping table on first use if they do not
            exist.  Set to ``False`` when migrations own DDL.
    """

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        dsn: str | None = None,
        graph_name: str = "astrocyte",
        *,
        bootstrap_schema: bool = True,
        **kwargs: Any,  # absorb extra config keys from YAML loader
    ) -> None:
        self._dsn = (
            dsn
            or os.environ.get("ASTROCYTE_AGE_DSN")
            or os.environ.get("DATABASE_URL")
        )
        if not self._dsn:
            raise ValueError(
                "AgeGraphStore requires `dsn`, DATABASE_URL, or ASTROCYTE_AGE_DSN"
            )
        self._graph = _safe_graph_name(graph_name)
        self._bootstrap_schema = bootstrap_schema
        self._pool: AsyncConnectionPool | None = None
        self._pool_lock = asyncio.Lock()
        self._schema_ready = not bootstrap_schema
        self._schema_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection pool
    # ------------------------------------------------------------------

    async def _ensure_pool(self) -> AsyncConnectionPool:
        async with self._pool_lock:
            if self._pool is None:
                async def configure(conn: psycopg.AsyncConnection) -> None:
                    # Load AGE and set search path on every new connection.
                    # ``public`` is placed first so plain SQL like
                    # ``CREATE TABLE IF NOT EXISTS astrocyte_entities`` and
                    # ``INSERT INTO astrocyte_entities`` always target the
                    # canonical migrated table — preventing duplicate tables
                    # across schemas. AGE Cypher functions are accessed via
                    # explicit ``ag_catalog.<fn>`` qualification in this
                    # module, so they don't need ag_catalog to be searched
                    # first; we still include it last so unqualified Cypher
                    # references continue to resolve.
                    await conn.execute("LOAD 'age'")
                    await conn.execute(
                        "SET search_path = public, \"$user\", ag_catalog"
                    )
                    await conn.commit()

                self._pool = AsyncConnectionPool(
                    conninfo=self._dsn,
                    configure=configure,
                    open=False,
                    min_size=2,
                    # Sized for the parallel retain pipeline + PgQueuer worker
                    # drain phase running concurrently. Each retain record can
                    # hold 2-3 connections (store_entities + link_memories +
                    # entity_resolver.resolve fanning out across candidates),
                    # and PgQueuer schedules persona-compile tasks unbounded
                    # by default, each of which acquires a connection. With
                    # batch_size=10 retain + ~10 PgQueuer in flight, 40 leaves
                    # comfortable headroom for occasional bursts.
                    max_size=40,
                    kwargs={"connect_timeout": 10},
                )
                await self._pool.open()
        return self._pool

    async def _conn(self) -> psycopg.AsyncConnection:
        pool = await self._ensure_pool()
        return await pool.getconn()

    async def _release(self, conn: psycopg.AsyncConnection) -> None:
        pool = await self._ensure_pool()
        await pool.putconn(conn)

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            conn = await self._conn()
            try:
                # Ensure the AGE extension exists (idempotent; requires superuser).
                # This is a no-op when migrations have already run 001_age_extension.sql.
                async with conn.cursor() as cur:
                    await cur.execute("CREATE EXTENSION IF NOT EXISTS age")
                    await cur.execute("LOAD 'age'")
                    # See pool ``configure`` callback for rationale on the
                    # search-path order. ``public`` first prevents plain-SQL
                    # writes from creating duplicate tables in ag_catalog.
                    await cur.execute(
                        "SET search_path = public, \"$user\", ag_catalog"
                    )

                # Create the graph if it doesn't exist yet.
                # AGE raises an error if you call create_graph twice,
                # so we check the catalog first.
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s",
                        [self._graph],
                    )
                    row = await cur.fetchone()
                    if row and row[0] == 0:
                        await cur.execute(
                            "SELECT ag_catalog.create_graph(%s)", [self._graph]
                        )

                    # Pre-create vertex and edge labels so concurrent MERGE
                    # operations never race to create them lazily. AGE raises
                    # an error if a label already exists, so guard with a
                    # catalog check first.
                    await cur.execute(
                        """
                        SELECT count(*) FROM ag_catalog.ag_label l
                        JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
                        WHERE g.name = %s AND l.name = 'Entity'
                        """,
                        [self._graph],
                    )
                    if (await cur.fetchone() or (0,))[0] == 0:
                        await cur.execute(
                            "SELECT ag_catalog.create_vlabel(%s, 'Entity')",
                            [self._graph],
                        )

                    await cur.execute(
                        """
                        SELECT count(*) FROM ag_catalog.ag_label l
                        JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
                        WHERE g.name = %s AND l.name = 'LINK'
                        """,
                        [self._graph],
                    )
                    if (await cur.fetchone() or (0,))[0] == 0:
                        await cur.execute(
                            "SELECT ag_catalog.create_elabel(%s, 'LINK')",
                            [self._graph],
                        )

                    # Memory-entity mapping table (plain SQL)
                    # Hindsight-parity memory-to-memory links table.
                    # Stores caused_by chains (extracted at retain time)
                    # and the precomputed semantic kNN graph (each new
                    # memory linked to top-K most-similar prior memories
                    # with similarity >= threshold). Used by the
                    # link-expansion retrieval CTE.
                    await cur.execute("""
                        CREATE TABLE IF NOT EXISTS astrocyte_memory_links (
                            id BIGSERIAL PRIMARY KEY,
                            bank_id TEXT NOT NULL,
                            source_memory_id TEXT NOT NULL,
                            target_memory_id TEXT NOT NULL,
                            link_type TEXT NOT NULL,
                            evidence TEXT,
                            confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                            weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            UNIQUE (bank_id, source_memory_id, target_memory_id, link_type)
                        )
                    """)
                    await cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_mem_links_source
                        ON astrocyte_memory_links (bank_id, source_memory_id)
                    """)
                    await cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_mem_links_target
                        ON astrocyte_memory_links (bank_id, target_memory_id)
                    """)
                    await cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_mem_links_type
                        ON astrocyte_memory_links (bank_id, link_type)
                    """)
                    await cur.execute("""
                        CREATE TABLE IF NOT EXISTS astrocyte_age_mem_entity (
                            id         BIGSERIAL PRIMARY KEY,
                            bank_id    TEXT        NOT NULL,
                            memory_id  TEXT        NOT NULL,
                            entity_id  TEXT        NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            UNIQUE (bank_id, memory_id, entity_id)
                        )
                    """)
                    await cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_age_mem_entity_bank_entity
                        ON astrocyte_age_mem_entity (bank_id, entity_id)
                    """)
                    await cur.execute("""
                        CREATE TABLE IF NOT EXISTS astrocyte_entities (
                            id TEXT NOT NULL,
                            bank_id TEXT NOT NULL,
                            name TEXT NOT NULL,
                            entity_type TEXT NOT NULL,
                            aliases TEXT[],
                            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                            mention_count BIGINT NOT NULL DEFAULT 1,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (bank_id, id)
                        )
                    """)
                    # Backfill column for existing deployments where the table
                    # was created before mention_count was introduced. Idempotent.
                    await cur.execute("""
                        ALTER TABLE astrocyte_entities
                            ADD COLUMN IF NOT EXISTS mention_count BIGINT NOT NULL DEFAULT 1
                    """)
                    await cur.execute("""
                        CREATE TABLE IF NOT EXISTS astrocyte_entity_links (
                            id BIGSERIAL PRIMARY KEY,
                            bank_id TEXT NOT NULL,
                            entity_a TEXT NOT NULL,
                            entity_b TEXT NOT NULL,
                            link_type TEXT NOT NULL,
                            evidence TEXT,
                            confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            UNIQUE (bank_id, entity_a, entity_b, link_type)
                        )
                    """)
                    await cur.execute("""
                        CREATE TABLE IF NOT EXISTS astrocyte_memory_entities (
                            bank_id TEXT NOT NULL,
                            memory_id TEXT NOT NULL,
                            entity_id TEXT NOT NULL,
                            evidence TEXT,
                            confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (bank_id, memory_id, entity_id)
                        )
                    """)
                    await cur.execute("""
                        CREATE INDEX IF NOT EXISTS astrocyte_memory_entities_entity_idx
                        ON astrocyte_memory_entities (bank_id, entity_id)
                    """)

                await conn.commit()
                self._schema_ready = True
            except Exception:
                await conn.rollback()
                raise
            finally:
                await self._release(conn)

    # ------------------------------------------------------------------
    # Cypher execution helper
    # ------------------------------------------------------------------

    async def _cypher(
        self,
        conn: psycopg.AsyncConnection,
        cypher: str,
        columns: list[str],
    ) -> list[dict[str, Any]]:
        """Execute a Cypher query via AGE and return rows as dicts.

        Args:
            cypher: Cypher query string (may contain inline-escaped values
                interpolated via ``_q()``).
            columns: Expected return column names, used to build the AS clause.
        """
        as_clause = ", ".join(f"{col} agtype" for col in columns)
        sql = f"SELECT * FROM ag_catalog.cypher('{self._graph}', $$ {cypher} $$) AS ({as_clause})"
        async with conn.cursor() as cur:
            try:
                await cur.execute(sql)
            except Exception as exc:
                _logger.debug("Cypher error: %s\nQuery: %s", exc, sql)
                raise
            rows = await cur.fetchall()
            col_names = [desc.name for desc in cur.description] if cur.description else columns
        return [dict(zip(col_names, row)) for row in rows]

    # ------------------------------------------------------------------
    # GraphStore SPI
    # ------------------------------------------------------------------

    async def store_entities(self, entities: list[Entity], bank_id: str) -> list[str]:
        """MERGE entity vertices into the graph.

        When an entity carries an ``embedding`` attribute, it is persisted in
        the ``astrocyte_entities.embedding`` column for use by the
        Hindsight-inspired entity-resolution cascade. The column is added by
        the ``009_entities_trigram_embedding.sql`` migration; deployments
        that haven't run that migration get a column-not-exists error at
        first write — re-run migrations to fix.

        Concurrency note: entities are sorted by ``id`` before iteration so
        concurrent ``store_entities`` calls (e.g. retain_many's parallel
        ``_process_record_entities``) acquire row-level locks in the same
        order across transactions. Without this, two records that
        independently resolve to overlapping canonical entities can lock
        them in opposite orders and deadlock at INSERT time. Sorting is
        cheap and eliminates the deadlock entirely.
        """
        await self._ensure_schema()
        conn = await self._conn()
        ids: list[str] = []
        # Stable lock-acquisition order across concurrent transactions.
        sorted_entities = sorted(entities, key=lambda e: e.id)
        # Preserve the caller's input order in the returned ID list so
        # downstream code (link_memories_to_entities, store_links) lines
        # up with the entities argument.
        original_order = [e.id for e in entities]
        try:
            for entity in sorted_entities:
                # Format embedding as a pgvector text literal `[1.0,2.0,...]`
                # — avoids needing pgvector type registration on the AGE pool.
                # ``None`` becomes SQL NULL; ``::vector`` cast accepts both.
                embedding_text: str | None = None
                if entity.embedding is not None:
                    embedding_text = (
                        "[" + ",".join(f"{x:.10g}" for x in entity.embedding) + "]"
                    )
                async with conn.cursor() as cur:
                    if embedding_text is not None:
                        # pgvector column present — include embedding in upsert.
                        await cur.execute(
                            """
                            INSERT INTO astrocyte_entities
                                (bank_id, id, name, entity_type, aliases, metadata,
                                 embedding, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s::vector, NOW())
                            ON CONFLICT (bank_id, id) DO UPDATE SET
                                name = EXCLUDED.name,
                                entity_type = EXCLUDED.entity_type,
                                aliases = EXCLUDED.aliases,
                                metadata = EXCLUDED.metadata,
                                embedding = COALESCE(EXCLUDED.embedding, astrocyte_entities.embedding),
                                updated_at = NOW()
                            """,
                            [
                                bank_id,
                                entity.id,
                                entity.name,
                                entity.entity_type or "OTHER",
                                entity.aliases,
                                Json(entity.metadata or {}),
                                embedding_text,
                            ],
                        )
                    else:
                        # No embedding supplied (AGE-only deployment without pgvector,
                        # or embedding not yet computed). Skip the embedding column so
                        # the INSERT works even if the column hasn't been added yet by
                        # migration 005_entities_trigram_embedding.sql.
                        await cur.execute(
                            """
                            INSERT INTO astrocyte_entities
                                (bank_id, id, name, entity_type, aliases, metadata,
                                 updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, NOW())
                            ON CONFLICT (bank_id, id) DO UPDATE SET
                                name = EXCLUDED.name,
                                entity_type = EXCLUDED.entity_type,
                                aliases = EXCLUDED.aliases,
                                metadata = EXCLUDED.metadata,
                                updated_at = NOW()
                            """,
                            [
                                bank_id,
                                entity.id,
                                entity.name,
                                entity.entity_type or "OTHER",
                                entity.aliases,
                                Json(entity.metadata or {}),
                            ],
                        )
                aliases_json = json.dumps(entity.aliases or [])
                etype = _q(entity.entity_type or "OTHER")
                cypher = (
                    f"MERGE (e:Entity {{id: '{_q(entity.id)}', bank: '{_q(bank_id)}'}}) "
                    f"SET e.name = '{_q(entity.name)}', "
                    f"    e.entity_type = '{etype}', "
                    f"    e.aliases = '{_q(aliases_json)}' "
                    f"RETURN e.id"
                )
                await self._cypher(conn, cypher, ["eid"])
                ids.append(entity.id)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await self._release(conn)
        # Restore caller's input order for the returned ID list so the
        # subsequent link-memories-to-entities call lines up.
        return original_order

    async def store_links(self, links: list[EntityLink], bank_id: str) -> list[str]:
        """MERGE relationship edges between entity vertices.

        Concurrency note: links are sorted by ``(entity_a, entity_b, link_type)``
        before iteration so concurrent transactions acquire row-level locks
        in the same order. Mirrors the deadlock guard in :meth:`store_entities`.
        """
        await self._ensure_schema()
        conn = await self._conn()
        link_ids: list[str] = []
        sorted_links = sorted(
            links, key=lambda link: (link.entity_a, link.entity_b, link.link_type)
        )
        try:
            for i, link in enumerate(sorted_links):
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO astrocyte_entity_links
                            (bank_id, entity_a, entity_b, link_type, evidence, confidence, metadata, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (bank_id, entity_a, entity_b, link_type) DO UPDATE SET
                            evidence = EXCLUDED.evidence,
                            confidence = EXCLUDED.confidence,
                            metadata = EXCLUDED.metadata
                        """,
                        [
                            bank_id,
                            link.entity_a,
                            link.entity_b,
                            link.link_type,
                            link.evidence,
                            float(link.confidence),
                            Json(link.metadata or {}),
                            link.created_at or datetime.now(timezone.utc),
                        ],
                    )
                cypher = (
                    f"MATCH (a:Entity {{id: '{_q(link.entity_a)}', bank: '{_q(bank_id)}'}}), "
                    f"      (b:Entity {{id: '{_q(link.entity_b)}', bank: '{_q(bank_id)}'}}) "
                    f"MERGE (a)-[r:LINK {{link_type: '{_q(link.link_type)}', bank: '{_q(bank_id)}'}}]->(b) "
                    f"SET r.confidence = {float(link.confidence)}, "
                    f"    r.evidence = '{_q(link.evidence)}' "
                    f"RETURN r.link_type"
                )
                try:
                    await self._cypher(conn, cypher, ["lt"])
                except Exception as exc:
                    _logger.warning(
                        "store_links: failed for %s->%s in bank %r: %s",
                        link.entity_a, link.entity_b, bank_id, exc,
                    )
                link_ids.append(f"lnk_{i}")
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await self._release(conn)
        return link_ids

    async def link_memories_to_entities(
        self,
        associations: list[MemoryEntityAssociation],
        bank_id: str,
    ) -> None:
        """Persist memory↔entity associations in the SQL mapping table."""
        await self._ensure_schema()
        if not associations:
            return
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                for assoc in associations:
                    await cur.execute(
                        """
                        INSERT INTO astrocyte_age_mem_entity (bank_id, memory_id, entity_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (bank_id, memory_id, entity_id) DO NOTHING
                        """,
                        [bank_id, assoc.memory_id, assoc.entity_id],
                    )
                    await cur.execute(
                        """
                        INSERT INTO astrocyte_memory_entities (bank_id, memory_id, entity_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (bank_id, memory_id, entity_id) DO NOTHING
                        """,
                        [bank_id, assoc.memory_id, assoc.entity_id],
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await self._release(conn)

    async def query_neighbors(
        self,
        entity_ids: list[str],
        bank_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> list[GraphHit]:
        """Find memories linked to these entity IDs via the mapping table."""
        await self._ensure_schema()
        if not entity_ids:
            return []
        conn = await self._conn()
        try:
            placeholders = ", ".join(["%s"] * len(entity_ids))
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT DISTINCT memory_id
                    FROM astrocyte_memory_entities
                    WHERE bank_id = %s AND entity_id IN ({placeholders})
                    LIMIT %s
                    """,
                    [bank_id, *entity_ids, limit],
                )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)

        return [
            GraphHit(
                memory_id=row[0],
                text="",
                connected_entities=list(entity_ids),
                depth=1,
                score=0.5,
            )
            for row in rows
        ]

    async def query_entities(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
    ) -> list[Entity]:
        """Search canonical SQL entities by name or alias."""
        await self._ensure_schema()
        conn = await self._conn()
        try:
            q_lower = query.strip().lower()
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, name, entity_type, aliases, metadata
                    FROM astrocyte_entities
                    WHERE bank_id = %s
                      AND (
                        lower(name) LIKE %s
                        OR EXISTS (
                            SELECT 1
                            FROM unnest(COALESCE(aliases, ARRAY[]::text[])) AS alias
                            WHERE lower(alias) LIKE %s
                        )
                      )
                    ORDER BY name
                    LIMIT %s
                    """,
                    [bank_id, f"%{q_lower}%", f"%{q_lower}%", limit],
                )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)

        entities: list[Entity] = []
        for row in rows:
            entities.append(Entity(
                id=str(row[0]),
                name=str(row[1]),
                entity_type=str(row[2] or "OTHER"),
                aliases=list(row[3]) if row[3] else None,
                metadata=dict(row[4]) if row[4] else None,
            ))
        return entities

    async def find_entity_candidates(
        self,
        name: str,
        bank_id: str,
        threshold: float = 0.8,
        limit: int = 5,
    ) -> list[Entity]:
        """Return entities whose name is a substring match for *name*.

        The AGE implementation uses Cypher ``CONTAINS`` (case-insensitive)
        as a proxy for similarity.  The *threshold* parameter is accepted
        for interface compatibility; production deployments can extend this
        with ``pg_trgm`` similarity scoring on the mapping table.
        """
        # Reuse query_entities with slightly higher limit to account for
        # the threshold not being applied, then truncate.
        return (await self.query_entities(name, bank_id, limit=limit * 2))[:limit]

    async def find_entity_candidates_scored(
        self,
        name: str,
        bank_id: str,
        *,
        name_embedding: list[float] | None = None,
        trigram_threshold: float = 0.15,
        limit: int = 10,
    ) -> list[EntityCandidateMatch]:
        """Score candidates with pg_trgm + cosine + cooccurrence + temporal.

        Implements the Hindsight-inspired retain-time entity-resolution
        cascade. Each candidate row carries four signals:

        1. ``pg_trgm.similarity()`` over ``lower(name)`` (the trigram tier).
        2. Cosine similarity between candidate's stored ``embedding`` column
           and the supplied ``name_embedding`` (computed in Python).
        3. ``co_occurring_names`` — names of entities linked via
           ``co_occurs`` to this candidate (from a single batched lateral
           join in the same SELECT).
        4. ``last_seen`` (= ``updated_at``) for the temporal-decay tier.

        Trigram threshold defaults to ``0.15`` to match Hindsight's
        empirically-tuned default; see ``entity_resolution.py``.
        """
        await self._ensure_schema()
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                # The lateral subquery collects the candidate's co-occurring
                # entity names in one round-trip. We deduplicate via DISTINCT
                # because (entity_a, entity_b) pairs can have multiple links
                # of varied types.
                await cur.execute(
                    """
                    SELECT
                        e.id,
                        e.name,
                        e.entity_type,
                        e.aliases,
                        e.metadata,
                        e.embedding::text,
                        e.updated_at,
                        e.mention_count,
                        similarity(lower(e.name), lower(%s)) AS name_sim,
                        COALESCE(
                            (
                                SELECT array_agg(DISTINCT lower(other.name))
                                FROM astrocyte_entity_links l
                                JOIN astrocyte_entities other
                                  ON other.bank_id = l.bank_id
                                 AND other.id = CASE
                                     WHEN l.entity_a = e.id THEN l.entity_b
                                     ELSE l.entity_a
                                 END
                                WHERE l.bank_id = e.bank_id
                                  AND l.link_type = 'co_occurs'
                                  AND (l.entity_a = e.id OR l.entity_b = e.id)
                            ),
                            ARRAY[]::text[]
                        ) AS co_occurring_names
                    FROM astrocyte_entities e
                    WHERE e.bank_id = %s
                      AND similarity(lower(e.name), lower(%s)) >= %s
                    ORDER BY name_sim DESC
                    LIMIT %s
                    """,
                    [name, bank_id, name, trigram_threshold, limit],
                )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)

        matches: list[EntityCandidateMatch] = []
        for row in rows:
            (
                eid,
                ename,
                etype,
                ealiases,
                emetadata,
                embedding_text,
                updated_at,
                mention_count,
                name_sim,
                co_occurring_names,
            ) = row

            cand_embedding: list[float] | None = None
            if embedding_text:
                # pgvector text format is `[1,2,3]`; strip brackets and split.
                try:
                    inner = embedding_text.strip().lstrip("[").rstrip("]")
                    if inner:
                        cand_embedding = [float(v) for v in inner.split(",")]
                except (ValueError, AttributeError):
                    cand_embedding = None

            emb_sim: float | None = None
            if name_embedding is not None and cand_embedding is not None:
                emb_sim = _cosine_sim(name_embedding, cand_embedding)
                emb_sim = max(0.0, min(1.0, emb_sim))

            entity = Entity(
                id=eid,
                name=ename,
                entity_type=etype,
                aliases=list(ealiases) if ealiases else None,
                metadata=dict(emetadata) if emetadata else None,
                embedding=cand_embedding,
                mention_count=int(mention_count) if mention_count is not None else 1,
            )
            matches.append(
                EntityCandidateMatch(
                    entity=entity,
                    name_similarity=float(name_sim),
                    embedding_similarity=emb_sim,
                    co_occurring_names=list(co_occurring_names) if co_occurring_names else [],
                    last_seen=updated_at,
                    mention_count=int(mention_count) if mention_count is not None else 1,
                )
            )

        matches.sort(
            key=lambda m: max(m.name_similarity, m.embedding_similarity or 0.0),
            reverse=True,
        )
        return matches

    async def get_entity_ids_for_memories(
        self,
        memory_ids: list[str],
        bank_id: str,
    ) -> dict[str, list[str]]:
        """Return ``{memory_id: [entity_id, ...]}`` for the supplied IDs.

        Single-query lookup against ``astrocyte_age_mem_entity``; used
        by the spreading-activation pipeline to seed seed-entity IDs
        directly from the association table without relying on hit
        metadata. Hindsight-parity path.
        """
        if not memory_ids:
            return {}
        await self._ensure_schema()
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT memory_id, entity_id
                    FROM astrocyte_age_mem_entity
                    WHERE bank_id = %s
                      AND memory_id = ANY(%s::text[])
                    """,
                    [bank_id, list(memory_ids)],
                )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        out: dict[str, list[str]] = {}
        for memory_id, entity_id in rows:
            out.setdefault(memory_id, []).append(entity_id)
        return out

    async def expand_entities_via_links(
        self,
        entity_ids: list[str],
        bank_id: str,
        *,
        max_hops: int = 2,
        link_types: list[str] | None = None,
    ) -> dict[str, int]:
        """Recursive CTE walk over entity-link edges (Hindsight parity).

        BFS from ``entity_ids`` along edges in ``astrocyte_entity_links``
        whose ``link_type`` is in ``link_types`` (default: ``co_occurs``),
        out to ``max_hops`` distance. Returns
        ``{entity_id: shortest_hop_distance}`` with ``0`` for the seed
        set.

        The CTE caps at ``max_hops`` and uses ``UNION`` (not ``UNION ALL``)
        so we don't emit duplicate (entity, distance) tuples for entities
        reachable via multiple paths. The ``MIN(depth)`` outer aggregation
        ensures the returned distance is the shortest path.
        """
        if not entity_ids:
            return {}
        accepted_types = list(link_types or ["co_occurs"])
        capped_hops = max(1, max_hops)
        await self._ensure_schema()
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    WITH RECURSIVE
                    seeds AS (
                        SELECT unnest(%s::text[]) AS entity_id, 0 AS depth
                    ),
                    walk(entity_id, depth) AS (
                        SELECT entity_id, depth FROM seeds
                        UNION
                        SELECT
                            CASE
                                WHEN l.entity_a = w.entity_id THEN l.entity_b
                                ELSE l.entity_a
                            END AS entity_id,
                            w.depth + 1 AS depth
                        FROM walk w
                        JOIN astrocyte_entity_links l
                          ON l.bank_id = %s
                         AND (l.entity_a = w.entity_id OR l.entity_b = w.entity_id)
                         AND l.link_type = ANY(%s::text[])
                        WHERE w.depth < %s
                    )
                    SELECT entity_id, MIN(depth) AS depth
                    FROM walk
                    GROUP BY entity_id
                    """,
                    [entity_ids, bank_id, accepted_types, capped_hops],
                )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return {row[0]: int(row[1]) for row in rows}

    async def increment_mention_counts(
        self,
        entity_ids: list[str],
        bank_id: str,
    ) -> None:
        """Bump ``mention_count`` for each canonical entity ID by 1.

        Called by the entity resolver after a successful canonical
        resolution (trigram / embedding / composite / LLM autolinks).
        Hindsight-parity popularity signal — feeds the resolver's
        composite score as a soft tiebreaker.

        Idempotency note: this method is fire-and-forget. Repeated
        invocations DO bump the counter — so callers must only invoke
        it once per resolution decision (the resolver guards this via
        its cascade flow).
        """
        if not entity_ids:
            return
        await self._ensure_schema()
        conn = await self._conn()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE astrocyte_entities
                       SET mention_count = mention_count + 1,
                           updated_at = NOW()
                     WHERE bank_id = %s
                       AND id = ANY(%s::text[])
                    """,
                    [bank_id, entity_ids],
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await self._release(conn)

    async def store_memory_links(
        self,
        links: list,  # list[MemoryLink]
        bank_id: str,
    ) -> list[str]:
        """Persist memory-to-memory links (Hindsight-parity).

        Idempotent via the ``UNIQUE (bank_id, source_memory_id,
        target_memory_id, link_type)`` constraint — re-running retain
        on the same chunks won't multiply edges. Confidence/weight
        are updated on conflict (latest wins).
        """
        if not links:
            return []
        await self._ensure_schema()
        conn = await self._conn()
        ids: list[str] = []
        try:
            async with conn.cursor() as cur:
                for link in links:
                    ts = (
                        link.created_at.isoformat()
                        if link.created_at
                        else datetime.now(timezone.utc).isoformat()
                    )
                    await cur.execute(
                        """
                        INSERT INTO astrocyte_memory_links
                            (bank_id, source_memory_id, target_memory_id,
                             link_type, evidence, confidence, weight,
                             metadata, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (bank_id, source_memory_id,
                                     target_memory_id, link_type)
                        DO UPDATE SET
                            evidence = EXCLUDED.evidence,
                            confidence = EXCLUDED.confidence,
                            weight = EXCLUDED.weight,
                            metadata = EXCLUDED.metadata
                        RETURNING id
                        """,
                        [
                            bank_id,
                            link.source_memory_id,
                            link.target_memory_id,
                            link.link_type,
                            link.evidence or "",
                            float(link.confidence),
                            float(link.weight),
                            Json(link.metadata or {}),
                            ts,
                        ],
                    )
                    row = await cur.fetchone()
                    if row:
                        ids.append(str(row[0]))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await self._release(conn)
        return ids

    async def find_memory_links(
        self,
        seed_memory_ids: list[str],
        bank_id: str,
        *,
        link_types: list[str] | None = None,
        limit: int = 200,
    ) -> list:  # list[MemoryLink]
        """Return memory links touching any seed in either direction."""
        if not seed_memory_ids:
            return []
        await self._ensure_schema()
        conn = await self._conn()
        try:
            type_filter = ""
            params: list = [bank_id, list(seed_memory_ids), list(seed_memory_ids)]
            if link_types:
                type_filter = "AND link_type = ANY(%s::text[])"
                params.append(list(link_types))
            params.append(int(limit))
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT source_memory_id, target_memory_id, link_type,
                           evidence, confidence, weight, metadata, created_at
                    FROM astrocyte_memory_links
                    WHERE bank_id = %s
                      AND (source_memory_id = ANY(%s::text[])
                           OR target_memory_id = ANY(%s::text[]))
                      {type_filter}
                    LIMIT %s
                    """,
                    params,
                )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)

        from astrocyte.types import MemoryLink
        return [
            MemoryLink(
                source_memory_id=row[0],
                target_memory_id=row[1],
                link_type=row[2],
                evidence=row[3] or "",
                confidence=float(row[4]) if row[4] is not None else 1.0,
                weight=float(row[5]) if row[5] is not None else 1.0,
                metadata=dict(row[6]) if row[6] else None,
                created_at=row[7],
            )
            for row in rows
        ]

    async def expand_memory_links_fast(
        self,
        seed_memory_ids: list[str],
        bank_id: str,
        *,
        params: Any,
    ) -> list[dict[str, Any]]:
        """Single-query link expansion over entity, semantic, and causal signals.

        The portable pipeline fallback calls ``get_entity_ids_for_memories``,
        ``query_neighbors``, and ``find_memory_links`` separately. This fast path
        performs the same Hindsight-style scoring in one SQL query against the
        AGE adapter's plain PostgreSQL helper tables.
        """
        if not seed_memory_ids:
            return []
        await self._ensure_schema()
        conn = await self._conn()
        semantic_types = list(params.semantic_link_types)
        causal_types = list(params.causal_link_types)
        all_link_types = semantic_types + causal_types
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    WITH
                    seeds AS (
                        SELECT unnest(%s::text[]) AS memory_id
                    ),
                    seed_entities AS (
                        SELECT DISTINCT entity_id
                        FROM astrocyte_age_mem_entity
                        WHERE bank_id = %s
                          AND memory_id = ANY(%s::text[])
                    ),
                    entity_overlap AS (
                        SELECT
                            me.memory_id,
                            count(DISTINCT me.entity_id)::int AS entity_overlap,
                            0.0::double precision AS semantic_total,
                            0.0::double precision AS causal_total,
                            ARRAY['entity_overlap']::text[] AS sources
                        FROM astrocyte_age_mem_entity me
                        JOIN seed_entities se ON se.entity_id = me.entity_id
                        WHERE me.bank_id = %s
                          AND NOT (me.memory_id = ANY(%s::text[]))
                        GROUP BY me.memory_id
                        ORDER BY entity_overlap DESC
                        LIMIT %s
                    ),
                    link_rows AS (
                        SELECT
                            CASE
                                WHEN ml.source_memory_id = ANY(%s::text[]) THEN ml.target_memory_id
                                ELSE ml.source_memory_id
                            END AS memory_id,
                            ml.link_type,
                            ml.weight
                        FROM astrocyte_memory_links ml
                        WHERE ml.bank_id = %s
                          AND ml.link_type = ANY(%s::text[])
                          AND (
                              ml.source_memory_id = ANY(%s::text[])
                              OR ml.target_memory_id = ANY(%s::text[])
                          )
                          AND NOT (
                              ml.source_memory_id = ANY(%s::text[])
                              AND ml.target_memory_id = ANY(%s::text[])
                          )
                    ),
                    semantic_links AS (
                        SELECT
                            memory_id,
                            0::int AS entity_overlap,
                            sum(weight)::double precision AS semantic_total,
                            0.0::double precision AS causal_total,
                            ARRAY['semantic']::text[] AS sources
                        FROM link_rows
                        WHERE link_type = ANY(%s::text[])
                          AND NOT (memory_id = ANY(%s::text[]))
                        GROUP BY memory_id
                    ),
                    causal_links AS (
                        SELECT
                            memory_id,
                            0::int AS entity_overlap,
                            0.0::double precision AS semantic_total,
                            sum(weight + 1.0)::double precision AS causal_total,
                            ARRAY['causal']::text[] AS sources
                        FROM link_rows
                        WHERE link_type = ANY(%s::text[])
                          AND NOT (memory_id = ANY(%s::text[]))
                        GROUP BY memory_id
                    ),
                    combined AS (
                        SELECT * FROM entity_overlap
                        UNION ALL
                        SELECT * FROM semantic_links
                        UNION ALL
                        SELECT * FROM causal_links
                    ),
                    scored AS (
                        SELECT
                            memory_id,
                            sum(entity_overlap)::int AS entity_overlap,
                            sum(semantic_total)::double precision AS semantic_total,
                            sum(causal_total)::double precision AS causal_total,
                            array_agg(DISTINCT source) AS sources
                        FROM combined
                        CROSS JOIN LATERAL unnest(sources) AS source
                        GROUP BY memory_id
                    )
                    SELECT
                        memory_id,
                        entity_overlap,
                        semantic_total,
                        causal_total,
                        sources,
                        (
                            %s * least(entity_overlap::double precision / 5.0, 1.0)
                            + %s * least(semantic_total, 1.0)
                            + %s * least(causal_total, 1.0)
                        ) AS total_score
                    FROM scored
                    WHERE (
                        %s * least(entity_overlap::double precision / 5.0, 1.0)
                        + %s * least(semantic_total, 1.0)
                        + %s * least(causal_total, 1.0)
                    ) >= %s
                    ORDER BY total_score DESC
                    LIMIT %s
                    """,
                    [
                        seed_memory_ids,
                        bank_id,
                        seed_memory_ids,
                        bank_id,
                        seed_memory_ids,
                        int(params.per_entity_limit) * max(1, len(seed_memory_ids)),
                        seed_memory_ids,
                        bank_id,
                        all_link_types,
                        seed_memory_ids,
                        seed_memory_ids,
                        seed_memory_ids,
                        seed_memory_ids,
                        semantic_types,
                        seed_memory_ids,
                        causal_types,
                        seed_memory_ids,
                        float(params.entity_overlap_weight),
                        float(params.semantic_weight),
                        float(params.causal_weight),
                        float(params.entity_overlap_weight),
                        float(params.semantic_weight),
                        float(params.causal_weight),
                        float(params.activation_threshold),
                        int(params.expansion_limit) * 2,
                    ],
                )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)

        return [
            {
                "memory_id": row[0],
                "entity_overlap": row[1],
                "semantic_total": row[2],
                "causal_total": row[3],
                "sources": list(row[4] or []),
                "total_score": row[5],
            }
            for row in rows
        ]

    async def store_entity_link(self, link: EntityLink, bank_id: str) -> str:
        """Persist a single resolved entity link (M11 entity resolution)."""
        await self._ensure_schema()
        conn = await self._conn()
        try:
            ts = (link.created_at or datetime.now(timezone.utc)).isoformat()
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO astrocyte_entity_links
                        (bank_id, entity_a, entity_b, link_type, evidence, confidence, metadata, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (bank_id, entity_a, entity_b, link_type) DO UPDATE SET
                        evidence = EXCLUDED.evidence,
                        confidence = EXCLUDED.confidence,
                        metadata = EXCLUDED.metadata
                    """,
                    [
                        bank_id,
                        link.entity_a,
                        link.entity_b,
                        link.link_type,
                        link.evidence,
                        float(link.confidence),
                        Json(link.metadata or {}),
                        link.created_at or datetime.now(timezone.utc),
                    ],
                )
            cypher = (
                f"MATCH (a:Entity {{id: '{_q(link.entity_a)}', bank: '{_q(bank_id)}'}}), "
                f"      (b:Entity {{id: '{_q(link.entity_b)}', bank: '{_q(bank_id)}'}}) "
                f"MERGE (a)-[r:LINK {{link_type: '{_q(link.link_type)}', bank: '{_q(bank_id)}'}}]->(b) "
                f"SET r.confidence = {float(link.confidence)}, "
                f"    r.evidence = '{_q(link.evidence)}', "
                f"    r.created_at = '{_q(ts)}' "
                f"RETURN id(r)"
            )
            rows = await self._cypher(conn, cypher, ["rid"])
            await conn.commit()
            rid = rows[0].get("rid", "") if rows else ""
            return _parse_scalar(rid) or f"link_{link.entity_a}_{link.entity_b}"
        except Exception:
            await conn.rollback()
            raise
        finally:
            await self._release(conn)

    async def health(self) -> HealthStatus:
        """Check AGE connectivity by querying the graph catalog."""
        try:
            await self._ensure_schema()
            pool = await self._ensure_pool()
            conn = await pool.getconn()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s",
                        [self._graph],
                    )
                    row = await cur.fetchone()
                graph_exists = row is not None and row[0] > 0
                msg = f"age ok (graph '{self._graph}' {'found' if graph_exists else 'not yet created'})"
                return HealthStatus(healthy=True, message=msg)
            finally:
                await pool.putconn(conn)
        except Exception as exc:
            return HealthStatus(healthy=False, message=str(exc))

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
