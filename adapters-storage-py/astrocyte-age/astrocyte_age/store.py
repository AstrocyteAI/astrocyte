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
  ``docker/postgres-age-pgvector/Dockerfile``).
- Run migrations in ``migrations/`` order before first use, or set
  ``bootstrap_schema=True`` (default) to auto-create on first connection.
- The database user needs ``SUPERUSER`` or ``pg_read_server_files`` to run
  ``LOAD 'age'``; or pre-load AGE in ``shared_preload_libraries``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, ClassVar

import psycopg
from astrocyte.types import (
    Entity,
    EntityLink,
    GraphHit,
    HealthStatus,
    MemoryEntityAssociation,
)
from psycopg_pool import AsyncConnectionPool

_logger = logging.getLogger("astrocyte.age")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IDENT_SAFE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


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
                    await conn.execute("LOAD 'age'")
                    await conn.execute(
                        "SET search_path = ag_catalog, \"$user\", public"
                    )
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
                    await cur.execute(
                        "SET search_path = ag_catalog, \"$user\", public"
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

                    # Memory-entity mapping table (plain SQL)
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
        """MERGE entity vertices into the graph."""
        await self._ensure_schema()
        conn = await self._conn()
        ids: list[str] = []
        try:
            for entity in entities:
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
        return ids

    async def store_links(self, links: list[EntityLink], bank_id: str) -> list[str]:
        """MERGE relationship edges between entity vertices."""
        await self._ensure_schema()
        conn = await self._conn()
        link_ids: list[str] = []
        try:
            for i, link in enumerate(links):
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
                    FROM astrocyte_age_mem_entity
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
        """Search for entities whose name contains the query string."""
        await self._ensure_schema()
        conn = await self._conn()
        try:
            q_lower = query.strip().lower()
            cypher = (
                f"MATCH (e:Entity {{bank: '{_q(bank_id)}'}}) "
                f"WHERE toLower(e.name) CONTAINS '{_q(q_lower)}' "
                f"RETURN e "
                f"LIMIT {int(limit)}"
            )
            rows = await self._cypher(conn, cypher, ["e"])
        finally:
            await self._release(conn)

        entities: list[Entity] = []
        for row in rows:
            props = _parse_agtype(row.get("e"))
            if not props:
                continue
            aliases_raw = props.get("aliases", "[]")
            try:
                aliases = json.loads(aliases_raw) if isinstance(aliases_raw, str) else aliases_raw
            except (json.JSONDecodeError, ValueError):
                aliases = []
            entities.append(Entity(
                id=_parse_scalar(props.get("id", "")),
                name=_parse_scalar(props.get("name", "")),
                entity_type=_parse_scalar(props.get("entity_type", "OTHER")),
                aliases=aliases or None,
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

    async def store_entity_link(self, link: EntityLink, bank_id: str) -> str:
        """Persist a single resolved entity link (M11 entity resolution)."""
        await self._ensure_schema()
        conn = await self._conn()
        try:
            ts = (link.created_at or datetime.now(timezone.utc)).isoformat()
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
