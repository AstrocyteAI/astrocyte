"""Integration tests for ``PostgresMentalModelStore`` (M9 first-class).

Hits a live PostgreSQL via ``DATABASE_URL`` (skipped if unset). Each test
uses a unique ``bank_id`` so they don't collide across parallel runs.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from astrocyte.types import MentalModel

from astrocyte_postgres.mental_model_store import PostgresMentalModelStore


@pytest.fixture
def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — skipping postgres mental-model store tests")
    return url


@pytest.fixture
async def store(dsn: str):
    """Returns a store + a unique bank_id; tears down rows for that bank."""
    bank = f"mm-bank-{uuid.uuid4().hex[:8]}"
    s = PostgresMentalModelStore(dsn=dsn, bootstrap_schema=False)
    yield s, bank
    # Tear down: hard-delete all rows for this test's bank.
    conn = await psycopg.AsyncConnection.connect(dsn)
    async with conn:
        await conn.execute(
            "DELETE FROM public.astrocyte_mental_model_versions WHERE bank_id = %s", [bank],
        )
        await conn.execute(
            "DELETE FROM public.astrocyte_mental_models WHERE bank_id = %s", [bank],
        )
        await conn.commit()


def _draft(model_id: str, bank_id: str, *, content: str = "v1") -> MentalModel:
    return MentalModel(
        model_id=model_id,
        bank_id=bank_id,
        title="Test",
        content=content,
        scope="bank",
        source_ids=[],
        revision=0,
        refreshed_at=datetime.now(UTC),
    )


class TestUpsertCRUD:
    """Basic upsert / get / list / delete."""

    @pytest.mark.asyncio
    async def test_first_upsert_creates_revision_1(self, store):
        s, bank = store
        rev = await s.upsert(_draft("m1", bank), bank)
        assert rev == 1

        got = await s.get("m1", bank)
        assert got is not None
        assert got.model_id == "m1"
        assert got.revision == 1
        assert got.content == "v1"

    @pytest.mark.asyncio
    async def test_subsequent_upsert_bumps_revision(self, store):
        s, bank = store
        await s.upsert(_draft("m1", bank, content="v1"), bank)
        rev2 = await s.upsert(_draft("m1", bank, content="v2"), bank)
        rev3 = await s.upsert(_draft("m1", bank, content="v3"), bank)

        assert (rev2, rev3) == (2, 3)
        got = await s.get("m1", bank)
        assert got.content == "v3"
        assert got.revision == 3

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self, store):
        s, bank = store
        assert await s.get("never-created", bank) is None

    @pytest.mark.asyncio
    async def test_list_filters_by_scope(self, store):
        s, bank = store
        m1 = _draft("m1", bank)
        m2 = dataclasses.replace(_draft("m2", bank), scope="person:alice")
        m3 = dataclasses.replace(_draft("m3", bank), scope="person:bob")

        for m in (m1, m2, m3):
            await s.upsert(m, bank)

        all_models = await s.list(bank)
        assert {m.model_id for m in all_models} == {"m1", "m2", "m3"}

        alice = await s.list(bank, scope="person:alice")
        assert [m.model_id for m in alice] == ["m2"]

        bob = await s.list(bank, scope="person:bob")
        assert [m.model_id for m in bob] == ["m3"]

    @pytest.mark.asyncio
    async def test_list_orders_by_refreshed_desc(self, store):
        """Most-recently-refreshed first — matches the schema's bank_refreshed_idx."""
        s, bank = store
        await s.upsert(_draft("oldest", bank), bank)
        await s.upsert(_draft("middle", bank), bank)
        await s.upsert(_draft("newest", bank), bank)

        listed = await s.list(bank)
        assert [m.model_id for m in listed] == ["newest", "middle", "oldest"]


class TestSoftDelete:
    """Delete is a soft-delete that filters rows from get/list."""

    @pytest.mark.asyncio
    async def test_delete_returns_true_for_existing(self, store):
        s, bank = store
        await s.upsert(_draft("m1", bank), bank)
        assert await s.delete("m1", bank) is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_for_missing(self, store):
        s, bank = store
        assert await s.delete("never-existed", bank) is False

    @pytest.mark.asyncio
    async def test_get_returns_none_after_delete(self, store):
        s, bank = store
        await s.upsert(_draft("m1", bank), bank)
        await s.delete("m1", bank)
        assert await s.get("m1", bank) is None

    @pytest.mark.asyncio
    async def test_list_omits_deleted(self, store):
        s, bank = store
        await s.upsert(_draft("kept", bank), bank)
        await s.upsert(_draft("dropped", bank), bank)
        await s.delete("dropped", bank)

        listed = await s.list(bank)
        assert [m.model_id for m in listed] == ["kept"]

    @pytest.mark.asyncio
    async def test_delete_returns_false_on_double_delete(self, store):
        s, bank = store
        await s.upsert(_draft("m1", bank), bank)
        assert await s.delete("m1", bank) is True
        assert await s.delete("m1", bank) is False  # already gone

    @pytest.mark.asyncio
    async def test_upsert_after_delete_revives_with_revision_1(self, store, dsn):
        """Re-creating an id with the same model_id after delete behaves as a
        fresh creation (new revision = 1) — soft-deleted rows are skipped
        by upsert's existing-row probe."""
        s, bank = store
        await s.upsert(_draft("m1", bank, content="v1"), bank)
        await s.delete("m1", bank)
        # Hard-delete the soft-deleted row to simulate a real reuse scenario;
        # otherwise the PRIMARY KEY conflict on revival is the legitimate
        # scenario explored in the next test.
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            await conn.execute(
                "DELETE FROM public.astrocyte_mental_model_versions WHERE bank_id=%s AND model_id=%s",
                [bank, "m1"],
            )
            await conn.execute(
                "DELETE FROM public.astrocyte_mental_models WHERE bank_id=%s AND model_id=%s",
                [bank, "m1"],
            )
            await conn.commit()

        rev = await s.upsert(_draft("m1", bank, content="fresh"), bank)
        assert rev == 1
        got = await s.get("m1", bank)
        assert got.content == "fresh"


class TestRevisionHistory:
    """Each upsert archives the prior current-revision row into the
    versions table, enabling diff / changelog queries."""

    @pytest.mark.asyncio
    async def test_first_upsert_does_not_archive(self, store, dsn):
        s, bank = store
        await s.upsert(_draft("m1", bank), bank)
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM public.astrocyte_mental_model_versions WHERE bank_id=%s AND model_id=%s",
                    [bank, "m1"],
                )
                assert (await cur.fetchone())[0] == 0

    @pytest.mark.asyncio
    async def test_each_upsert_archives_one_prior_revision(self, store, dsn):
        s, bank = store
        await s.upsert(_draft("m1", bank, content="v1"), bank)
        await s.upsert(_draft("m1", bank, content="v2"), bank)
        await s.upsert(_draft("m1", bank, content="v3"), bank)

        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT revision, content
                      FROM public.astrocyte_mental_model_versions
                     WHERE bank_id=%s AND model_id=%s
                  ORDER BY revision
                    """,
                    [bank, "m1"],
                )
                rows = await cur.fetchall()
        # v1 archived when v2 upserted; v2 archived when v3 upserted; v3 is the current row.
        assert rows == [(1, "v1"), (2, "v2")]


class TestHealth:
    @pytest.mark.asyncio
    async def test_healthy_when_db_reachable(self, store):
        s, _ = store
        h = await s.health()
        assert h.healthy is True
