"""Integration tests for Neo4j GraphStore."""

from __future__ import annotations

import os

import pytest
from astrocyte.types import Entity, MemoryEntityAssociation

from astrocyte_neo4j import Neo4jGraphStore

NEO4J_URI = os.environ.get("ASTROCYTE_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("ASTROCYTE_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("ASTROCYTE_NEO4J_PASSWORD", "testpass")

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.mark.asyncio
async def test_health(require_neo4j: None) -> None:
    store = Neo4jGraphStore(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASSWORD)
    h = await store.health()
    assert h.healthy is True


@pytest.mark.asyncio
async def test_entities_link_neighbors(require_neo4j: None) -> None:
    store = Neo4jGraphStore(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASSWORD)
    bank = "neo4j_test_bank"
    ents = [
        Entity(id="e1", name="Acme Corp", entity_type="ORG"),
    ]
    ids = await store.store_entities(ents, bank_id=bank)
    assert ids == ["e1"]

    await store.link_memories_to_entities(
        [MemoryEntityAssociation(memory_id="m1", entity_id="e1")],
        bank_id=bank,
    )

    hits = await store.query_neighbors(["e1"], bank_id=bank, limit=5)
    assert len(hits) >= 1
    assert hits[0].memory_id == "m1"

    found = await store.query_entities("Acme", bank_id=bank, limit=5)
    assert len(found) >= 1
    assert found[0].id == "e1"
