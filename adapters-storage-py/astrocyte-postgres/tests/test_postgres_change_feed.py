"""PostgresStore.list_changes (team-memory change feed) and migration 039.

The SQLite adapter's parity suite checks list_changes against this store with
randomised operations; these pin the Postgres side on its own: the generated
``changed_at`` column, tombstones, keyset paging, byte-wise id order whatever
the database locale, and migration 039's index (``changed_at`` is an indexed
expression, so rows written before the migration need no backfill).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from astrocyte_postgres.store import _CHANGED_AT, PostgresStore

from .conftest import make_item

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "039_vectors_changed_at.sql"


async def _ids(store: PostgresStore, bank: str = "bank-1", **kw) -> list[tuple[str, bool]]:
    return [(c.id, c.deleted) for c in await store.list_changes(bank, **kw)]


class TestListChanges:
    async def test_order_paging_and_record(self, store: PostgresStore):
        items = [make_item(f"m{i:02d}", retained_at=T0, metadata={"k": i}, tags=["x"]) for i in range(10)]
        items += [
            make_item("early", retained_at=T0 - timedelta(hours=1)),
            make_item("late", retained_at=T0 + timedelta(hours=1)),
        ]
        items.append(make_item("elsewhere", bank_id="bank-2", retained_at=T0))
        await store.store_vectors(items)
        expected = ["early", *(f"m{i:02d}" for i in range(10)), "late"]
        assert [i for i, _ in await _ids(store)] == expected
        for limit in (1, 4, 12, 100):
            seen, after = [], None
            while page := await store.list_changes("bank-1", after=after, limit=limit):
                seen += [c.id for c in page]
                after = (page[-1].changed_at, page[-1].id)
            assert seen == expected, limit
        m3 = next(c for c in await store.list_changes("bank-1") if c.id == "m03")
        assert (m3.changed_at, m3.retained_at, m3.text, m3.metadata, m3.tags) == (T0, T0, "test text", {"k": 3}, ["x"])
        assert await store.list_changes("bank-1", limit=0) == []

    async def test_ids_order_byte_wise_not_by_locale(self, store: PostgresStore):
        ids = ["Zeta", "alpha", "_u", "-d", "B-2", "b_1"]
        await store.store_vectors([make_item(i, retained_at=T0) for i in ids])
        assert [i for i, _ in await _ids(store)] == sorted(ids)  # Python str order = code-point order
        assert [i for i, _ in await _ids(store, after=(T0, "Zeta"))] == [i for i in sorted(ids) if i > "Zeta"]

    async def test_forget_tombstone_and_restore(self, store: PostgresStore):
        await store.store_vectors([make_item("a", retained_at=T0), make_item("b", retained_at=T0)])
        assert await store.delete(["a"], "bank-1") == 1
        changes = await store.list_changes("bank-1")
        assert [(c.id, c.deleted) for c in changes] == [("b", False), ("a", True)]
        tomb = changes[1]
        assert tomb.text is None and tomb.metadata is None and tomb.retained_at is None
        assert tomb.changed_at > T0  # forgotten now; max(retained_at, forgotten_at)
        # Re-storing the id (upsert clears forgotten_at) makes it live again.
        later = datetime.now(UTC) + timedelta(days=1)
        await store.store_vectors([make_item("a", retained_at=later)])
        assert (await _ids(store))[-1] == ("a", False)
        assert (await store.list_changes("bank-1"))[-1].changed_at == later


class TestMigration039:
    """Applies the migration file to a pre-039 ``astrocyte_vectors`` in a scratch schema."""

    @pytest.fixture
    async def scratch(self, dsn: str):
        schema = f"mig039_{uuid.uuid4().hex[:8]}"
        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        try:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(f'SET search_path = "{schema}", public')
            yield conn, schema
        finally:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.close()

    async def test_existing_rows_are_in_the_feed_through_the_index(self, scratch):
        conn, schema = scratch
        # The columns 039 reads, as earlier migrations left them (002 + 006).
        await conn.execute(
            """
            CREATE TABLE astrocyte_vectors (
                id TEXT PRIMARY KEY,
                bank_id TEXT NOT NULL,
                text TEXT NOT NULL,
                retained_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                forgotten_at TIMESTAMPTZ
            )
            """
        )
        rows = [
            ("live", T0, None),
            ("gone", T0 - timedelta(hours=2), T0 + timedelta(hours=3)),
            ("skew", T0 + timedelta(hours=5), T0 + timedelta(hours=4)),
        ]
        for mid, retained, forgotten in rows:
            await conn.execute(
                "INSERT INTO astrocyte_vectors (id, bank_id, text, retained_at, forgotten_at) "
                "VALUES (%s, 'b', 't', %s, %s)",
                (mid, retained, forgotten),
            )

        sql = MIGRATION.read_text(encoding="utf-8")
        await conn.execute(sql)
        await conn.execute(sql)  # idempotent

        feed_sql = f"""
            SELECT id, {_CHANGED_AT} AS changed_at FROM astrocyte_vectors
            WHERE bank_id = 'b' ORDER BY {_CHANGED_AT}, id COLLATE "C"
        """
        cur = await conn.execute(feed_sql)
        assert await cur.fetchall() == [
            ("live", T0),
            ("gone", T0 + timedelta(hours=3)),
            ("skew", T0 + timedelta(hours=5)),
        ]
        # Later writes are reflected without touching any changed_at column.
        forget = "UPDATE astrocyte_vectors SET forgotten_at = %s WHERE id = 'live'"
        await conn.execute(forget, (T0 + timedelta(days=1),))
        cur = await conn.execute(feed_sql)
        assert (await cur.fetchall())[-1] == ("live", T0 + timedelta(days=1))

        cur = await conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = %s AND indexname = 'astrocyte_vectors_bank_changed_idx'",
            (schema,),
        )
        [(indexdef,)] = await cur.fetchall()
        assert 'GREATEST(retained_at, forgotten_at)' in indexdef and 'COLLATE "C"' in indexdef
        # The store's keyset query is served by it (seqscan off: the table is tiny).
        await conn.execute("SET enable_seqscan = off")
        cur = await conn.execute(
            f"""EXPLAIN SELECT id FROM astrocyte_vectors WHERE bank_id = 'b'
                AND ({_CHANGED_AT}, id COLLATE "C") > (%s, %s)
                ORDER BY {_CHANGED_AT}, id COLLATE "C" LIMIT 10""",
            (T0, "a"),
        )
        plan = "\n".join(r[0] for r in await cur.fetchall())
        assert "astrocyte_vectors_bank_changed_idx" in plan, plan
