"""SqliteStore.insert_vectors / lookup_ids: the store side of team-memory push (G2).

Ids are unique across the whole file (``id TEXT UNIQUE``), so an insert-only
write must never take over a row of another bank or revive a forgotten one.
Parity with Postgres is in test_parity_postgres.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from astrocyte.types import VectorItem

from astrocyte_sqlite import SqliteStore

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _item(id_: str, bank: str = "b", text: str = "t", at: datetime = T0) -> VectorItem:
    return VectorItem(id=id_, bank_id=bank, vector=[1.0, 0.0], text=text, metadata={"k": 1}, retained_at=at)


async def test_insert_only_never_overwrites(tmp_path):
    store = SqliteStore(path=str(tmp_path / "m.db"))
    await store.store_vectors([_item("live"), _item("gone"), _item("theirs", bank="other", text="secret")])
    await store.delete(["gone"], "b")
    inserted = await store.insert_vectors(
        [
            _item("new1", text="fresh"),
            _item("live", text="overwrite attempt"),
            _item("gone", text="revive attempt"),
            _item("theirs", text="takeover attempt"),
            _item("new2", text="fresh too", at=T0 + timedelta(seconds=1)),
        ]
    )
    assert inserted == ["new1", "new2"]
    states = {c.id: c for c in await store.lookup_ids(["live", "gone", "theirs", "new1", "new2"])}
    assert states["live"].text == "t" and not states["live"].deleted
    assert states["gone"].deleted and states["gone"].text is None
    assert (states["theirs"].bank_id, states["theirs"].text) == ("other", "secret")
    assert states["new1"].text == "fresh" and states["new1"].changed_at == T0
    assert await store.insert_vectors([]) == []


async def test_lookup_ids_spans_banks_keeps_request_order_and_skips_unknown(tmp_path):
    store = SqliteStore(path=str(tmp_path / "m.db"))
    await store.store_vectors([_item("a", bank="b1"), _item("b", bank="b2")])
    found = await store.lookup_ids(["b", "missing", "a", "b"])
    assert [(c.id, c.bank_id) for c in found] == [("b", "b2"), ("a", "b1")]
    assert await store.lookup_ids([]) == []
