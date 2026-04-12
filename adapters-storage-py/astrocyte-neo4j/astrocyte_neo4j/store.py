"""Async Neo4j implementation of :class:`~astrocyte.provider.GraphStore`."""

from __future__ import annotations

from typing import ClassVar

from astrocyte.types import (
    Entity,
    EntityLink,
    GraphHit,
    HealthStatus,
    MemoryEntityAssociation,
)
from neo4j import AsyncGraphDatabase


class Neo4jGraphStore:
    """Graph store using labeled nodes per bank (``bank`` property isolation)."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        *,
        database: str | None = None,
    ) -> None:
        self._driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        self._database = database or "neo4j"

    async def store_entities(self, entities: list[Entity], bank_id: str) -> list[str]:
        out: list[str] = []
        async with self._driver.session(database=self._database) as session:
            for e in entities:
                await session.run(
                    """
                    MERGE (n:AstrocyteEntity {entity_id: $eid, bank: $bank})
                    SET n.name = $name,
                        n.entity_type = $etype,
                        n.aliases = $aliases
                    """,
                    eid=e.id,
                    bank=bank_id,
                    name=e.name,
                    etype=e.entity_type,
                    aliases=e.aliases or [],
                )
                out.append(e.id)
        return out

    async def store_links(self, links: list[EntityLink], bank_id: str) -> list[str]:
        ids: list[str] = []
        async with self._driver.session(database=self._database) as session:
            for i, link in enumerate(links):
                await session.run(
                    """
                    MATCH (s:AstrocyteEntity {entity_id: $sid, bank: $bank})
                    MATCH (t:AstrocyteEntity {entity_id: $tid, bank: $bank})
                    MERGE (s)-[r:ENTITY_LINK {link_type: $lt}]->(t)
                    SET r.metadata = $meta
                    """,
                    sid=link.source_entity_id,
                    tid=link.target_entity_id,
                    bank=bank_id,
                    lt=link.link_type,
                    meta=dict(link.metadata or {}),
                )
                ids.append(f"lnk_{i}")
        return ids

    async def link_memories_to_entities(
        self,
        associations: list[MemoryEntityAssociation],
        bank_id: str,
    ) -> None:
        async with self._driver.session(database=self._database) as session:
            for a in associations:
                await session.run(
                    """
                    MERGE (m:AstrocyteMemory {memory_id: $mid, bank: $bank})
                    ON CREATE SET m.text = ''
                    WITH m
                    MATCH (e:AstrocyteEntity {entity_id: $eid, bank: $bank})
                    MERGE (m)-[:MENTIONS]->(e)
                    """,
                    mid=a.memory_id,
                    eid=a.entity_id,
                    bank=bank_id,
                )

    async def query_neighbors(
        self,
        entity_ids: list[str],
        bank_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> list[GraphHit]:
        del max_depth  # v1: single hop memory ↔ entity; deeper graph expansion later
        if not entity_ids:
            return []
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                """
                MATCH (m:AstrocyteMemory {bank: $bank})-[:MENTIONS]->(e:AstrocyteEntity {bank: $bank})
                WHERE e.entity_id IN $eids
                RETURN DISTINCT m.memory_id AS mid, m.text AS txt
                LIMIT $lim
                """,
                bank=bank_id,
                eids=entity_ids,
                lim=limit,
            )
            hits: list[GraphHit] = []
            async for rec in result:
                mid = str(rec["mid"] or "")
                txt = str(rec["txt"] or "") or f"[graph result for {mid}]"
                hits.append(
                    GraphHit(
                        memory_id=mid,
                        text=txt,
                        connected_entities=list(entity_ids),
                        depth=1,
                        score=0.5,
                    ),
                )
            return hits

    async def query_entities(self, query: str, bank_id: str, limit: int = 10) -> list[Entity]:
        q = query.strip().lower()
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                """
                MATCH (n:AstrocyteEntity {bank: $bank})
                WHERE toLower(n.name) CONTAINS $q
                RETURN n.entity_id AS id, n.name AS name, n.entity_type AS etype, n.aliases AS aliases
                LIMIT $lim
                """,
                bank=bank_id,
                q=q,
                lim=limit,
            )
            out: list[Entity] = []
            async for rec in result:
                out.append(
                    Entity(
                        id=str(rec["id"]),
                        name=str(rec["name"]),
                        entity_type=str(rec["etype"] or "OTHER"),
                        aliases=list(rec["aliases"] or []) if rec["aliases"] is not None else None,
                    ),
                )
            return out

    async def health(self) -> HealthStatus:
        try:
            await self._driver.verify_connectivity()
            return HealthStatus(healthy=True, message="neo4j ok")
        except Exception as e:
            return HealthStatus(healthy=False, message=str(e))
