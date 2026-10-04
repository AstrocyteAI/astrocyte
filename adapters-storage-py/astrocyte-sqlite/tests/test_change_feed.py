"""SqliteStore.list_changes (team-memory change feed) and the ``changed_at`` upgrade.

Query parity with Postgres is in test_parity_postgres.py. These cover what is
SQLite-specific: the schema upgrade of a database created before
``changed_at`` existed, and that purge (the local erase) leaves no tombstone.
Timestamps are explicit wherever order matters (Windows clocks tick ~15 ms).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from astrocyte.types import VectorItem

from astrocyte_sqlite import SqliteStore
from astrocyte_sqlite.store import _to_us

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _item(id_: str, at: datetime = T0, bank: str = "b", text: str | None = None) -> VectorItem:
    return VectorItem(
        id=id_,
        bank_id=bank,
        vector=[1.0, 0.0],
        text=text or f"memory {id_}",
        metadata={"k": 1},
        tags=["x"],
        fact_type="world",
        occurred_at=at - timedelta(days=1),
        retained_at=at,
    )


async def _ids(store: SqliteStore, bank: str = "b", **kw) -> list[tuple[str, bool]]:
    return [(c.id, c.deleted) for c in await store.list_changes(bank, **kw)]


async def test_orders_by_changed_at_then_id_and_resumes_strictly_after(tmp_path):
    store = SqliteStore(path=str(tmp_path / "m.db"))
    await store.store_vectors([_item(f"m{i:02d}") for i in range(12)])
    await store.store_vectors([_item("early", T0 - timedelta(hours=1)), _item("late", T0 + timedelta(hours=1))])
    await store.store_vectors([_item("elsewhere", bank="other")])
    expected = ["early", *(f"m{i:02d}" for i in range(12)), "late"]
    assert [i for i, _ in await _ids(store)] == expected
    for limit in (1, 5, 13, 50):
        seen, after = [], None
        while page := await store.list_changes("b", after=after, limit=limit):
            seen += [c.id for c in page]
            after = (page[-1].changed_at, page[-1].id)
        assert seen == expected, limit
    assert await store.list_changes("b", limit=0) == []


async def test_live_rows_carry_the_record(tmp_path):
    store = SqliteStore(path=str(tmp_path / "m.db"))
    await store.store_vectors([_item("a")])
    [change] = await store.list_changes("b")
    assert (change.id, change.bank_id, change.changed_at, change.retained_at) == ("a", "b", T0, T0)
    assert (change.text, change.metadata, change.tags, change.fact_type) == ("memory a", {"k": 1}, ["x"], "world")
    assert change.occurred_at == T0 - timedelta(days=1) and not change.deleted


async def test_forget_is_a_tombstone_and_a_restore_is_live_again(tmp_path):
    store = SqliteStore(path=str(tmp_path / "m.db"))
    await store.store_vectors([_item("a"), _item("b")])
    assert await store.delete(["a"], "b") == 1
    changes = await store.list_changes("b")
    assert [(c.id, c.deleted) for c in changes] == [("b", False), ("a", True)]
    tomb = changes[1]
    assert tomb.text is None and tomb.metadata is None and tomb.retained_at is None
    assert tomb.changed_at > T0  # max(retained_at, forgotten_at) = when it was forgotten
    # A tombstone made "before" a future-dated row still sorts by max(): its retained_at.
    future = datetime.now(UTC) + timedelta(days=1)
    await store.store_vectors([_item("f", future)])
    await store.delete(["f"], "b")
    assert (await store.list_changes("b"))[-1].changed_at == future
    # Re-storing a forgotten id (upsert) makes it live again at its new retained_at.
    later = future + timedelta(days=1)
    await store.store_vectors([_item("a", later)])
    assert (await store.list_changes("b"))[-1].id == "a"
    assert not (await store.list_changes("b"))[-1].deleted


async def test_purge_erases_without_a_tombstone(tmp_path):
    # The local store's `memory forget` erases; the gateway (Postgres) soft-deletes.
    store = SqliteStore(path=str(tmp_path / "m.db"))
    await store.store_vectors([_item("a"), _item("b")])
    await store.delete(["a"], "b")
    assert await store.purge("b", ["a"]) == 1
    assert await _ids(store) == [("b", False)]


async def test_upgrades_a_database_created_before_changed_at(tmp_path):
    """A store file from an earlier version gains the column, a backfill and the index."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE astrocyte_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE astrocyte_vectors (
            pk INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, bank_id TEXT NOT NULL,
            embedding BLOB NOT NULL, text TEXT NOT NULL, metadata TEXT, tags TEXT,
            fact_type TEXT, occurred_at INTEGER, memory_layer TEXT,
            retained_at INTEGER NOT NULL, forgotten_at INTEGER, chunk_id TEXT
        );
        """
    )
    blob = bytes(8)  # two float32 zeros
    rows = [
        ("live", T0, None),
        ("gone", T0 - timedelta(hours=2), T0 + timedelta(hours=3)),
        ("odd", T0 + timedelta(hours=5), T0 + timedelta(hours=4)),  # forgotten "before" retained (clock skew)
    ]
    conn.executemany(
        "INSERT INTO astrocyte_vectors (id, bank_id, embedding, text, retained_at, forgotten_at) "
        "VALUES (?, 'b', ?, ?, ?, ?)",
        [(i, blob, f"text {i}", _to_us(r), _to_us(f)) for i, r, f in rows],
    )
    conn.execute("INSERT INTO astrocyte_meta VALUES ('embedding_dimensions', '2')")
    conn.commit()
    conn.close()

    store = SqliteStore(path=str(path))
    changes = await store.list_changes("b")
    assert [(c.id, c.deleted, c.changed_at) for c in changes] == [
        ("live", False, T0),
        ("gone", True, T0 + timedelta(hours=3)),
        ("odd", True, T0 + timedelta(hours=5)),
    ]

    conn = sqlite3.connect(path)
    try:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(astrocyte_vectors)")]
        indexes = [r[1] for r in conn.execute("PRAGMA index_list(astrocyte_vectors)")]
    finally:
        conn.close()
    assert columns.count("changed_at") == 1
    assert "astrocyte_vectors_bank_changed" in indexes

    # Reopening is a no-op, and writes through the new code keep the column.
    reopened = SqliteStore(path=str(path))
    await reopened.store_vectors([_item("new", T0 + timedelta(hours=6))])
    assert [c.id for c in await reopened.list_changes("b")] == ["live", "gone", "odd", "new"]
