"""Integration tests for AgeGraphStore — require a live AGE database.

All tests are auto-skipped unless ASTROCYTE_AGE_TEST_DSN is set (see conftest.py).
"""

from __future__ import annotations

import pytest
from astrocyte.types import Entity, EntityLink, MemoryEntityAssociation

from astrocyte_age.store import AgeGraphStore


def test_exposes_sql_link_expansion_fast_path() -> None:
    assert hasattr(AgeGraphStore, "expand_memory_links_fast")


@pytest.fixture
async def store(age_dsn: str) -> AgeGraphStore:
    s = AgeGraphStore(dsn=age_dsn, graph_name="astrocyte_test", bootstrap_schema=True)
    yield s
    await s.close()


class TestStoreEntities:
    @pytest.mark.asyncio
    async def test_store_and_query(self, store: AgeGraphStore):
        entity = Entity(id="alice_1", name="Alice", entity_type="PERSON")
        ids = await store.store_entities([entity], "bank1")
        assert "alice_1" in ids

        results = await store.query_entities("Alice", "bank1")
        assert any(e.name == "Alice" for e in results)

    @pytest.mark.asyncio
    async def test_merge_is_idempotent(self, store: AgeGraphStore):
        entity = Entity(id="bob_1", name="Bob", entity_type="PERSON")
        await store.store_entities([entity], "bank1")
        await store.store_entities([entity], "bank1")  # second call should not error

        results = await store.query_entities("Bob", "bank1")
        assert sum(1 for e in results if e.id == "bob_1") == 1


class TestStoreLinks:
    @pytest.mark.asyncio
    async def test_store_link(self, store: AgeGraphStore):
        ea = Entity(id="e_a", name="Alice", entity_type="PERSON")
        eb = Entity(id="e_b", name="Alice Smith", entity_type="PERSON")
        await store.store_entities([ea, eb], "bank1")

        link = EntityLink(entity_a="e_a", entity_b="e_b", link_type="alias_of", confidence=0.9)
        ids = await store.store_links([link], "bank1")
        assert len(ids) == 1


class TestMemoryEntityMap:
    @pytest.mark.asyncio
    async def test_link_and_query_neighbors(self, store: AgeGraphStore):
        entity = Entity(id="ent_1", name="Alice", entity_type="PERSON")
        await store.store_entities([entity], "bank1")

        await store.link_memories_to_entities(
            [MemoryEntityAssociation(memory_id="mem_1", entity_id="ent_1")],
            "bank1",
        )

        hits = await store.query_neighbors(["ent_1"], "bank1", limit=10)
        assert any(h.memory_id == "mem_1" for h in hits)


class TestFindCandidates:
    @pytest.mark.asyncio
    async def test_find_by_substring(self, store: AgeGraphStore):
        await store.store_entities([Entity(id="c1", name="Calvin the CTO", entity_type="PERSON")], "bank1")
        candidates = await store.find_entity_candidates("Calvin", "bank1")
        assert any(e.id == "c1" for e in candidates)


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_ok(self, store: AgeGraphStore):
        status = await store.health()
        assert status.healthy
