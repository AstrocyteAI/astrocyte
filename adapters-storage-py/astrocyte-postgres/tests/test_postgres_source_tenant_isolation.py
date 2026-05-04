"""End-to-end isolation tests for ``PostgresSourceStore`` schema-per-tenant.

A single store instance, two distinct tenant schemas via ``use_schema``;
neither tenant can see the other's documents or chunks. Mirrors the
existing isolation tests for PostgresStore + PostgresMentalModelStore.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from astrocyte.tenancy import use_schema
from astrocyte.types import SourceChunk, SourceDocument

from astrocyte_postgres.source_store import PostgresSourceStore


@pytest.fixture
def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    return url


@pytest.fixture
async def two_tenant_store(dsn: str):
    suffix = uuid.uuid4().hex[:8]
    schema_a = f"src_iso_a_{suffix}"
    schema_b = f"src_iso_b_{suffix}"
    store = PostgresSourceStore(dsn=dsn)
    yield store, schema_a, schema_b
    conn = await psycopg.AsyncConnection.connect(dsn)
    async with conn:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_a}" CASCADE')
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_b}" CASCADE')
        await conn.commit()


def _doc(doc_id: str, *, content_hash: str | None = None, title: str = "T") -> SourceDocument:
    return SourceDocument(
        id=doc_id,
        bank_id="bank-1",
        title=title,
        source_uri=f"memory://{doc_id}",
        content_hash=content_hash,
        content_type="text/plain",
        metadata={},
    )


def _chunk(chunk_id: str, document_id: str, *, chunk_index: int = 0,
           content_hash: str | None = None) -> SourceChunk:
    return SourceChunk(
        id=chunk_id,
        bank_id="bank-1",
        document_id=document_id,
        chunk_index=chunk_index,
        text=f"text-{chunk_id}",
        content_hash=content_hash,
        metadata={},
    )


class TestDocumentIsolation:
    @pytest.mark.asyncio
    async def test_writes_isolate_by_schema(self, two_tenant_store):
        store, schema_a, schema_b = two_tenant_store

        # Same id in both tenants — would collide under a shared schema;
        # must stay separate per-tenant.
        with use_schema(schema_a):
            await store.store_document(_doc("d-shared", title="alpha"))
        with use_schema(schema_b):
            await store.store_document(_doc("d-shared", title="beta"))

        with use_schema(schema_a):
            got_a = await store.get_document("d-shared", "bank-1")
        assert got_a.title == "alpha"

        with use_schema(schema_b):
            got_b = await store.get_document("d-shared", "bank-1")
        assert got_b.title == "beta"

    @pytest.mark.asyncio
    async def test_list_isolates_by_schema(self, two_tenant_store):
        store, schema_a, schema_b = two_tenant_store

        with use_schema(schema_a):
            await store.store_document(_doc("d-a-only"))
        with use_schema(schema_b):
            await store.store_document(_doc("d-b-only"))

        with use_schema(schema_a):
            assert {d.id for d in await store.list_documents("bank-1")} == {"d-a-only"}
        with use_schema(schema_b):
            assert {d.id for d in await store.list_documents("bank-1")} == {"d-b-only"}

    @pytest.mark.asyncio
    async def test_dedup_isolates_by_schema(self, two_tenant_store):
        """Same content_hash in two tenants does NOT dedup across tenants —
        the partial unique index lives in each tenant's schema."""
        store, schema_a, schema_b = two_tenant_store

        with use_schema(schema_a):
            id_a = await store.store_document(_doc("d-a", content_hash="hash-X"))
        with use_schema(schema_b):
            id_b = await store.store_document(_doc("d-b", content_hash="hash-X"))

        # Each tenant gets its own row (same hash, different schemas).
        assert id_a == "d-a"
        assert id_b == "d-b"

    @pytest.mark.asyncio
    async def test_delete_in_one_schema_does_not_affect_other(self, two_tenant_store):
        store, schema_a, schema_b = two_tenant_store

        with use_schema(schema_a):
            await store.store_document(_doc("shared", title="a"))
        with use_schema(schema_b):
            await store.store_document(_doc("shared", title="b"))

        with use_schema(schema_a):
            await store.delete_document("shared", "bank-1")
            assert await store.get_document("shared", "bank-1") is None

        with use_schema(schema_b):
            still = await store.get_document("shared", "bank-1")
            assert still is not None
            assert still.title == "b"


class TestChunkIsolation:
    @pytest.mark.asyncio
    async def test_chunks_isolate_by_schema(self, two_tenant_store):
        """Each tenant's chunks live in its own schema's chunks table;
        list_chunks(doc) in one tenant must never return the other's
        chunks even when document_id collides."""
        store, schema_a, schema_b = two_tenant_store

        with use_schema(schema_a):
            await store.store_document(_doc("d1"))
            await store.store_chunks([
                _chunk("c-a", "d1", chunk_index=0, content_hash="h-shared"),
            ])

        with use_schema(schema_b):
            await store.store_document(_doc("d1"))
            await store.store_chunks([
                _chunk("c-b", "d1", chunk_index=0, content_hash="h-shared"),
            ])

        with use_schema(schema_a):
            a_chunks = await store.list_chunks("d1", "bank-1")
        with use_schema(schema_b):
            b_chunks = await store.list_chunks("d1", "bank-1")

        assert [c.id for c in a_chunks] == ["c-a"]
        assert [c.id for c in b_chunks] == ["c-b"]

    @pytest.mark.asyncio
    async def test_per_schema_chunk_table_holds_one_row_each(self, two_tenant_store, dsn):
        """Sharper assertion — verify at the SQL layer that each tenant's
        chunks table holds exactly one row, proving the writes did NOT
        collide on a shared table."""
        store, schema_a, schema_b = two_tenant_store

        with use_schema(schema_a):
            await store.store_document(_doc("d1"))
            await store.store_chunks([_chunk("c-a", "d1", chunk_index=0)])
        with use_schema(schema_b):
            await store.store_document(_doc("d1"))
            await store.store_chunks([_chunk("c-b", "d1", chunk_index=0)])

        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f'SELECT count(*) FROM "{schema_a}".astrocyte_source_chunks'
                )
                assert (await cur.fetchone())[0] == 1
                await cur.execute(
                    f'SELECT count(*) FROM "{schema_b}".astrocyte_source_chunks'
                )
                assert (await cur.fetchone())[0] == 1
