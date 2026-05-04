"""End-to-end isolation tests for ``PostgresMentalModelStore`` schema-per-tenant.

A single store instance, two distinct tenant schemas via ``use_schema``;
neither tenant can see the other's mental models. Mirrors the existing
isolation tests for PostgresStore + AGE.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from astrocyte.tenancy import use_schema
from astrocyte.types import MentalModel

from astrocyte_postgres.mental_model_store import PostgresMentalModelStore


@pytest.fixture
def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    return url


@pytest.fixture
async def two_tenant_store(dsn: str):
    suffix = uuid.uuid4().hex[:8]
    schema_a = f"mm_iso_a_{suffix}"
    schema_b = f"mm_iso_b_{suffix}"
    store = PostgresMentalModelStore(dsn=dsn)
    yield store, schema_a, schema_b
    conn = await psycopg.AsyncConnection.connect(dsn)
    async with conn:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_a}" CASCADE')
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_b}" CASCADE')
        await conn.commit()


def _model(model_id: str, *, content: str) -> MentalModel:
    return MentalModel(
        model_id=model_id,
        bank_id="bank-1",
        title="T",
        content=content,
        scope="bank",
        source_ids=[],
        revision=0,
        refreshed_at=datetime.now(UTC),
    )


class TestSchemaIsolation:
    @pytest.mark.asyncio
    async def test_writes_isolate_by_schema(self, two_tenant_store):
        store, schema_a, schema_b = two_tenant_store

        with use_schema(schema_a):
            await store.upsert(_model("m-shared", content="alpha"), "bank-1")

        with use_schema(schema_b):
            await store.upsert(_model("m-shared", content="beta"), "bank-1")

        with use_schema(schema_a):
            got_a = await store.get("m-shared", "bank-1")
        assert got_a.content == "alpha"

        with use_schema(schema_b):
            got_b = await store.get("m-shared", "bank-1")
        assert got_b.content == "beta"

    @pytest.mark.asyncio
    async def test_list_isolates_by_schema(self, two_tenant_store):
        store, schema_a, schema_b = two_tenant_store

        with use_schema(schema_a):
            await store.upsert(_model("m-a-only", content="x"), "bank-1")
        with use_schema(schema_b):
            await store.upsert(_model("m-b-only", content="x"), "bank-1")

        with use_schema(schema_a):
            assert {m.model_id for m in await store.list("bank-1")} == {"m-a-only"}
        with use_schema(schema_b):
            assert {m.model_id for m in await store.list("bank-1")} == {"m-b-only"}

    @pytest.mark.asyncio
    async def test_delete_in_one_schema_does_not_affect_other(self, two_tenant_store):
        store, schema_a, schema_b = two_tenant_store

        with use_schema(schema_a):
            await store.upsert(_model("shared", content="x"), "bank-1")
        with use_schema(schema_b):
            await store.upsert(_model("shared", content="y"), "bank-1")

        with use_schema(schema_a):
            await store.delete("shared", "bank-1")
            assert await store.get("shared", "bank-1") is None

        with use_schema(schema_b):
            still_there = await store.get("shared", "bank-1")
            assert still_there is not None
            assert still_there.content == "y"

    @pytest.mark.asyncio
    async def test_revision_history_isolates_by_schema(self, two_tenant_store, dsn):
        """Per-revision history table also lives in the tenant's schema."""
        store, schema_a, schema_b = two_tenant_store

        with use_schema(schema_a):
            await store.upsert(_model("m1", content="v1"), "bank-1")
            await store.upsert(_model("m1", content="v2"), "bank-1")
        with use_schema(schema_b):
            await store.upsert(_model("m1", content="vX"), "bank-1")

        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f'SELECT count(*) FROM "{schema_a}".astrocyte_mental_model_versions'
                )
                a_versions = (await cur.fetchone())[0]
                await cur.execute(
                    f'SELECT count(*) FROM "{schema_b}".astrocyte_mental_model_versions'
                )
                b_versions = (await cur.fetchone())[0]

        # A archived 1 (v1 → when v2 was upserted); B archived 0 (only one upsert).
        assert (a_versions, b_versions) == (1, 0)
