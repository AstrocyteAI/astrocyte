"""End-to-end isolation tests for ``AgeGraphStore`` schema-per-tenant.

A single ``AgeGraphStore`` instance is exercised under two distinct tenant
schemas via :func:`astrocyte.tenancy.use_schema`. Each tenant gets:

- A dedicated AGE graph (``astrocyte__<schema>``)
- Dedicated SQL helper tables in the active Postgres schema

Neither tenant can see the other's entities, memory associations, or graph
edges. Default-schema (no ``use_schema``) behavior must remain backward-
compatible — uses the unmangled base graph name ``astrocyte`` and ``public``
schema.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from astrocyte.tenancy import DEFAULT_SCHEMA, _current_schema, get_current_schema, use_schema
from astrocyte.types import Entity, MemoryEntityAssociation

from astrocyte_age.store import AgeGraphStore


@pytest.fixture
async def two_tenant_store(age_dsn: str):
    """One AgeGraphStore, two distinct tenant schemas (random per test).

    Teardown drops both schemas (CASCADE) AND both per-tenant AGE graphs.
    """
    suffix = uuid.uuid4().hex[:8]
    schema_a = f"tenant_age_a_{suffix}"
    schema_b = f"tenant_age_b_{suffix}"
    base_graph = f"astrocyte_isol_{suffix}"

    store = AgeGraphStore(dsn=age_dsn, graph_name=base_graph)
    yield store, schema_a, schema_b, base_graph

    # Teardown: drop both tenant schemas + both AGE graphs.
    conn = await psycopg.AsyncConnection.connect(age_dsn)
    async with conn:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_a}" CASCADE')
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_b}" CASCADE')
        # AGE: drop graph if exists. ``drop_graph(name, true)`` is cascade.
        await conn.execute("LOAD 'age'")
        for graph in (f"{base_graph}__{schema_a}", f"{base_graph}__{schema_b}"):
            try:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT ag_catalog.drop_graph(%s, true)",
                        [graph],
                    )
            except Exception:
                pass  # graph wasn't created — fine
        await conn.commit()


def _entity(eid: str, name: str) -> Entity:
    return Entity(
        id=eid,
        name=name,
        entity_type="person",
        aliases=[],
        metadata={},
    )


def _assoc(memory_id: str, entity_id: str) -> MemoryEntityAssociation:
    return MemoryEntityAssociation(memory_id=memory_id, entity_id=entity_id)


class TestAgeIsolation:
    """Two tenants writing through one AgeGraphStore must stay isolated."""

    @pytest.mark.asyncio
    async def test_active_graph_differs_per_schema(self, two_tenant_store):
        store, schema_a, schema_b, base_graph = two_tenant_store

        with use_schema(schema_a):
            assert store._active_graph() == f"{base_graph}__{schema_a}"
        with use_schema(schema_b):
            assert store._active_graph() == f"{base_graph}__{schema_b}"
        # Default schema keeps the unmangled base name (backward-compat invariant).
        _current_schema.set(None)
        assert get_current_schema() == DEFAULT_SCHEMA
        assert store._active_graph() == base_graph

    @pytest.mark.asyncio
    async def test_entities_isolate_by_schema(self, two_tenant_store):
        """Each tenant's entities live in its own schema's helper tables AND
        its own AGE graph; reads from one tenant must never see the other's.

        Verified at the SQL helper-table layer because that's the observable
        cross-tenant boundary — the bootstrap path doesn't set up the
        ``embedding`` column from migration 009 (which is owned by the
        entity-resolution module), so we steer clear of that code path.
        """
        store, schema_a, schema_b, _ = two_tenant_store

        # Tenant A: store one entity.
        with use_schema(schema_a):
            await store.store_entities([_entity("alice-A", "Alice")], "bank-1")

        # Tenant B: store a different entity.
        with use_schema(schema_b):
            await store.store_entities([_entity("bob-B", "Bob")], "bank-1")

        # Verify each tenant's helper-table contains ONLY its own entity.
        async with await psycopg.AsyncConnection.connect(store._dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f'SELECT id FROM "{schema_a}".astrocyte_entities ORDER BY id'
                )
                a_ids = [r[0] for r in await cur.fetchall()]
                await cur.execute(
                    f'SELECT id FROM "{schema_b}".astrocyte_entities ORDER BY id'
                )
                b_ids = [r[0] for r in await cur.fetchall()]

        assert a_ids == ["alice-A"]
        assert b_ids == ["bob-B"]

    @pytest.mark.asyncio
    async def test_memory_associations_isolate_by_schema(self, two_tenant_store):
        store, schema_a, schema_b, _ = two_tenant_store

        # Same entity ID and same memory ID in both tenants — would collide
        # under a shared schema; must stay separate per-tenant.
        with use_schema(schema_a):
            await store.store_entities([_entity("shared", "Shared")], "bank-1")
            await store.link_memories_to_entities(
                [_assoc("mem-1", "shared")], "bank-1",
            )

        with use_schema(schema_b):
            await store.store_entities([_entity("shared", "Shared")], "bank-1")
            await store.link_memories_to_entities(
                [_assoc("mem-1", "shared")], "bank-1",
            )

        # Tenant A: lookup memory→entities for the shared memory id. Both
        # tenants stored a row keyed (mem-1, shared); each must see only
        # its own.
        with use_schema(schema_a):
            a_memories = await store.get_entity_ids_for_memories(["mem-1"], "bank-1")
        assert a_memories == {"mem-1": ["shared"]}

        with use_schema(schema_b):
            b_memories = await store.get_entity_ids_for_memories(["mem-1"], "bank-1")
        assert b_memories == {"mem-1": ["shared"]}

        # Sharper assertion: each tenant's SQL helper-table contains exactly
        # one row, proving the writes did NOT collide on a shared table.
        async with await psycopg.AsyncConnection.connect(store._dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f'SELECT count(*) FROM "{schema_a}".astrocyte_age_mem_entity'
                )
                assert (await cur.fetchone())[0] == 1
                await cur.execute(
                    f'SELECT count(*) FROM "{schema_b}".astrocyte_age_mem_entity'
                )
                assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_default_schema_uses_unmangled_graph(self, age_dsn: str):
        """Single-schema deployments must keep using the base graph name —
        backward compat for everyone running without a tenant extension."""
        from astrocyte.tenancy import _current_schema

        _current_schema.set(None)
        graph = f"astrocyte_default_{uuid.uuid4().hex[:8]}"
        store = AgeGraphStore(dsn=age_dsn, graph_name=graph)

        try:
            # Without a use_schema block, the active graph must be the bare
            # base name (no double-underscore tenant suffix).
            assert store._active_graph() == graph
            # And the bootstrap+store path must work end-to-end.
            await store.store_entities([_entity("e1", "DefaultEntity")], "bank-1")
            # Verify it actually wrote to public.astrocyte_entities (not a
            # tenant-scoped schema).
            async with await psycopg.AsyncConnection.connect(age_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id FROM public.astrocyte_entities WHERE id = 'e1'"
                    )
                    assert (await cur.fetchone()) is not None
        finally:
            # Clean up: remove the entity from public, drop the AGE graph.
            conn = await psycopg.AsyncConnection.connect(age_dsn)
            async with conn:
                await conn.execute(
                    "DELETE FROM public.astrocyte_entities WHERE id = 'e1'"
                )
                await conn.execute("LOAD 'age'")
                try:
                    async with conn.transaction():
                        await conn.execute("SELECT ag_catalog.drop_graph(%s, true)", [graph])
                except Exception:
                    pass
                await conn.commit()
