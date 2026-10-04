"""PostgresStore.insert_vectors / lookup_ids: the store side of team-memory push (G2).

``id`` is the table's primary key across banks, so the insert-only write must
never take over another bank's row or revive a forgotten one. Parity with
SQLite is in the astrocyte-sqlite suite.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg

from astrocyte_postgres.store import PostgresStore

from .conftest import make_item

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


async def test_insert_only_never_overwrites(store: PostgresStore, dsn: str):
    await store.store_vectors(
        [
            make_item("live", text="t", retained_at=T0),
            make_item("gone", text="t", retained_at=T0),
            make_item("theirs", bank_id="other", text="secret", vector=[0.0, 0.0, 1.0], retained_at=T0),
        ]
    )
    await store.delete(["gone"], "bank-1")
    inserted = await store.insert_vectors(
        [
            make_item("new1", text="fresh", retained_at=T0, metadata={"_actor": "user:alice"}),
            make_item("live", text="overwrite attempt"),
            make_item("gone", text="revive attempt"),
            make_item("theirs", text="takeover attempt", vector=[1.0, 0.0, 0.0]),
        ]
    )
    assert inserted == ["new1"]
    states = {c.id: c for c in await store.lookup_ids(["live", "gone", "theirs", "new1"])}
    assert states["live"].text == "t" and not states["live"].deleted
    assert states["gone"].deleted and states["gone"].text is None
    assert (states["theirs"].bank_id, states["theirs"].text) == ("other", "secret")
    assert (states["new1"].text, states["new1"].changed_at, states["new1"].metadata) == (
        "fresh",
        T0,
        {"_actor": "user:alice"},
    )
    theirs = [i for i in await store.list_vectors("other") if i.id == "theirs"]
    assert theirs[0].vector == [0.0, 0.0, 1.0]  # embedding untouched too
    # Side tables follow an insert, not a skipped one.
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        cur = await conn.execute("SELECT 1 FROM astrocyte_banks WHERE id = 'bank-1'")
        assert await cur.fetchone() == (1,)


async def test_lookup_ids_spans_banks_and_skips_unknown(store: PostgresStore):
    await store.store_vectors([make_item("a", bank_id="b1"), make_item("b", bank_id="b2")])
    found = await store.lookup_ids(["b", "missing", "a", "b"])
    assert [(c.id, c.bank_id) for c in found] == [("b", "b2"), ("a", "b1")]
    assert await store.lookup_ids([]) == []
