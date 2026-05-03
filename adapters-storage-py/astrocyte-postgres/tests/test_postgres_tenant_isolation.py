"""End-to-end isolation tests for schema-per-tenant.

Boots two tenants against the SAME ``PostgresStore`` instance, ingests
distinct memory into each via :func:`astrocyte.tenancy.use_schema`, and
verifies neither tenant can see the other's data.

This is the canary for the schema-per-tenant feature: if it ever fails,
something in the SQL layer has a hardcoded schema reference that bypasses
``self._fq()``.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from astrocyte.tenancy import use_schema

from astrocyte_postgres.store import PostgresStore

from .conftest import DIM, make_item


@pytest.fixture
def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — skipping integration test")
    return url


@pytest.fixture
async def two_tenant_store(dsn: str):
    """One PostgresStore instance, two distinct tenant schemas.

    Uses random schema names so the test is hermetic regardless of prior
    test state. Schema names are validated by ``fq_table`` so they must
    match the strict identifier regex.
    """
    suffix = uuid.uuid4().hex[:8]
    schema_a = f"tenant_iso_a_{suffix}"
    schema_b = f"tenant_iso_b_{suffix}"

    store = PostgresStore(dsn=dsn, table_name="astrocyte_vectors", embedding_dimensions=DIM)
    yield store, schema_a, schema_b

    # Teardown: drop both schemas (CASCADE removes all tables/indexes/triggers).
    conn = await psycopg.AsyncConnection.connect(dsn)
    async with conn:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_a}" CASCADE')
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_b}" CASCADE')
        await conn.commit()


class TestSchemaIsolation:
    """Two tenants writing through one store must never see each other's data."""

    @pytest.mark.asyncio
    async def test_writes_isolate_by_schema(self, two_tenant_store):
        store, schema_a, schema_b = two_tenant_store

        # Tenant A: store one item.
        with use_schema(schema_a):
            await store.store_vectors([make_item("A:item-1", text="alpha tenant content")])

        # Tenant B: store a different item.
        with use_schema(schema_b):
            await store.store_vectors([make_item("B:item-1", text="beta tenant content")])

        # Tenant A reads — must see ONLY its own item.
        with use_schema(schema_a):
            a_items = await store.list_vectors("bank-1")
        assert {item.id for item in a_items} == {"A:item-1"}

        # Tenant B reads — must see ONLY its own item.
        with use_schema(schema_b):
            b_items = await store.list_vectors("bank-1")
        assert {item.id for item in b_items} == {"B:item-1"}

    @pytest.mark.asyncio
    async def test_search_isolates_by_schema(self, two_tenant_store):
        store, schema_a, schema_b = two_tenant_store

        # Both tenants store an item with the SAME id and SAME query vector.
        # Without schema isolation, the second store would replace the first
        # via PRIMARY KEY conflict (or the search would return both).
        with use_schema(schema_a):
            await store.store_vectors([make_item("shared-id", text="alpha", vector=[1.0, 0.0, 0.0])])
        with use_schema(schema_b):
            await store.store_vectors([make_item("shared-id", text="beta", vector=[1.0, 0.0, 0.0])])

        # Search in A — must return A's text.
        with use_schema(schema_a):
            a_hits = await store.search_similar([1.0, 0.0, 0.0], "bank-1", limit=10)
        assert len(a_hits) == 1
        assert a_hits[0].text == "alpha"

        # Search in B — must return B's text.
        with use_schema(schema_b):
            b_hits = await store.search_similar([1.0, 0.0, 0.0], "bank-1", limit=10)
        assert len(b_hits) == 1
        assert b_hits[0].text == "beta"

    @pytest.mark.asyncio
    async def test_keyword_search_isolates_by_schema(self, two_tenant_store):
        store, schema_a, schema_b = two_tenant_store

        with use_schema(schema_a):
            await store.store_vectors([make_item("A:fts", text="quick brown fox jumps")])
        with use_schema(schema_b):
            await store.store_vectors([make_item("B:fts", text="quick brown fox jumps")])

        # Keyword search in A — must return only A's hit.
        with use_schema(schema_a):
            a_hits = await store.search_fulltext("quick brown", "bank-1", limit=10)
        assert {h.document_id for h in a_hits} == {"A:fts"}

        with use_schema(schema_b):
            b_hits = await store.search_fulltext("quick brown", "bank-1", limit=10)
        assert {h.document_id for h in b_hits} == {"B:fts"}

    @pytest.mark.asyncio
    async def test_delete_in_one_schema_does_not_affect_other(self, two_tenant_store):
        store, schema_a, schema_b = two_tenant_store

        with use_schema(schema_a):
            await store.store_vectors([make_item("shared-id", text="alpha")])
        with use_schema(schema_b):
            await store.store_vectors([make_item("shared-id", text="beta")])

        # Delete in A.
        with use_schema(schema_a):
            await store.delete(["shared-id"], "bank-1")
            assert await store.list_vectors("bank-1") == []

        # B's row must still be there.
        with use_schema(schema_b):
            b_items = await store.list_vectors("bank-1")
            assert len(b_items) == 1
            assert b_items[0].text == "beta"

    @pytest.mark.asyncio
    async def test_default_schema_is_public_when_no_use_schema(self, dsn: str):
        """Outside any ``use_schema`` block, all writes/reads target ``public``.

        This protects existing single-schema deployments from accidental
        breakage by the schema-per-tenant feature.
        """
        from astrocyte.tenancy import _current_schema, get_current_schema

        # Reset to ensure no leaked context from prior tests.
        _current_schema.set(None)
        assert get_current_schema() == "public"

        # Use a unique table name so we don't collide with the live bench data.
        table = f"test_default_{uuid.uuid4().hex[:8]}"
        store = PostgresStore(dsn=dsn, table_name=table, embedding_dimensions=DIM)

        try:
            await store.store_vectors([make_item("default-1", text="default schema item")])
            items = await store.list_vectors("bank-1")
            assert {item.id for item in items} == {"default-1"}
        finally:
            # Clean up the test table.
            conn = await psycopg.AsyncConnection.connect(dsn)
            async with conn:
                await conn.execute(f'DROP TABLE IF EXISTS public."{table}" CASCADE')
                await conn.commit()
