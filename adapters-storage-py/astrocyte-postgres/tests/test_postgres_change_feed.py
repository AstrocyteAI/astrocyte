"""PostgresStore.list_changes (team-memory change feed) and migration 039.

The SQLite adapter's parity suite checks list_changes against this store with
randomised operations; these pin the Postgres side on its own: ``changed_at``
is bumped by every write that changes a row (insert, overwrite, metadata
rewrite, forget, restore), tombstones, keyset paging, byte-wise id order
whatever the database locale, and migration 039 (rows written before it read
as max(retained_at, forgotten_at) through the indexed expression, so the
migration rewrites no rows).
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

    async def test_every_change_to_a_row_moves_it_to_the_end(self, store: PostgresStore):
        """The feed is every change to a synced row: a later rewrite of an
        existing row (here metadata, keeping the old retained_at, as the
        temporal-normalisation task does) shows up as an upsert of the same id
        after any cursor already handed out."""
        await store.store_vectors([make_item("a", retained_at=T0), make_item("b", retained_at=T0)])
        [_, b] = await store.list_changes("bank-1")
        cursor = (b.changed_at, b.id)
        await store.store_vectors([make_item("a", retained_at=T0, metadata={"status": "stale"})])
        [a] = await store.list_changes("bank-1", after=cursor)
        assert a.id == "a" and a.metadata == {"status": "stale"}
        assert a.retained_at == T0 and a.changed_at > T0


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
        # The column was added NULL: no row was rewritten.
        cur = await conn.execute("SELECT count(*) FROM astrocyte_vectors WHERE changed_at IS NOT NULL")
        assert (await cur.fetchone())[0] == 0
        # A write that sets the column takes precedence over the fallback.
        await conn.execute(
            "UPDATE astrocyte_vectors SET changed_at = %s WHERE id = 'live'", (T0 + timedelta(days=1),)
        )
        cur = await conn.execute(feed_sql)
        assert (await cur.fetchall())[-1] == ("live", T0 + timedelta(days=1))

        cur = await conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = %s AND indexname = 'astrocyte_vectors_bank_changed_idx'",
            (schema,),
        )
        [(indexdef,)] = await cur.fetchall()
        assert "COALESCE(changed_at, GREATEST(retained_at, forgotten_at))" in indexdef
        assert 'COLLATE "C"' in indexdef
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


class TestErase:
    """Erase (team memory G4): the forgotten row and its text leave the
    table; the tombstone stays in the feed and the id stays forgotten."""

    async def test_erase_keeps_the_tombstone_and_the_id_forgotten(self, store: PostgresStore):
        await store.store_vectors([
            make_item("a", retained_at=T0), make_item("b", retained_at=T0 + timedelta(seconds=1)),
            make_item("c", retained_at=T0 + timedelta(seconds=3)),
        ])
        await store.delete(["b"], "bank-1")
        assert await store.erase("bank-1", ["a", "b"]) == 1, "only forgotten rows are erased"
        assert await _ids(store) == [("a", False), ("c", False), ("b", True)]
        assert [c.id for c in await store.list_changes("bank-1", after=(T0, "a"), limit=1)] == ["c"]
        assert [c.id for c in await store.list_changes("bank-1", after=(T0, "a"), limit=2)] == ["c", "b"]
        assert [(c.id, c.deleted) for c in await store.lookup_ids(["b", "a"])] == [("b", True), ("a", False)]
        assert await store.insert_vectors([make_item("b", retained_at=T0 + timedelta(seconds=9))]) == []
        pool = await store._ensure_pool()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(f"SELECT count(*) FROM {store._fq()} WHERE id = 'b'")
            assert (await cur.fetchone())[0] == 0, "the row, with its text and embedding, is gone"

    async def test_erase_is_scoped_to_the_bank(self, store: PostgresStore):
        await store.store_vectors([make_item("x", bank_id="bank-2", retained_at=T0)])
        await store.delete(["x"], "bank-2")
        assert await store.erase("bank-1", ["x"]) == 0
        assert await store.erase("bank-2", ["x"]) == 1

    async def test_list_banks(self, store: PostgresStore):
        await store.store_vectors([make_item("a", retained_at=T0), make_item("b", retained_at=T0),
                                   make_item("c", bank_id="bank-2", retained_at=T0)])
        await store.delete(["b"], "bank-1")
        assert [(b, n) for b, n, _ in await store.list_banks()] == [("bank-1", 1), ("bank-2", 1)]


class TestLegalHolds:
    async def test_save_list_replace_delete(self, store: PostgresStore):
        from astrocyte.types import LegalHold

        await store.save_legal_hold(LegalHold(hold_id="case-7", bank_id="bank-1", reason="litigation",
                                              set_at=T0, set_by="user:counsel"))
        await store.save_legal_hold(LegalHold(hold_id="case-7", bank_id="bank-1", reason="extended",
                                              set_at=T0, set_by="user:counsel"))
        [hold] = await store.list_legal_holds("bank-1")
        assert (hold.hold_id, hold.reason, hold.set_at) == ("case-7", "extended", T0)
        assert await store.list_legal_holds("bank-2") == []
        assert await store.delete_legal_hold("bank-1", "case-7") is True
        assert await store.delete_legal_hold("bank-1", "case-7") is False
