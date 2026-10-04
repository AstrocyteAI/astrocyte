"""SqliteStore behaviour that Postgres parity cannot cover.

Parity (test_parity_postgres.py) proves the query semantics. These tests cover
what is specific to running from a local file: persistence, several processes
writing at once (an MCP server plus short-lived agent hooks), embedding
dimension pinning, the no-FTS5 fallback, and hostile keyword input.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest
from astrocyte.types import VectorFilters, VectorItem

from astrocyte_sqlite import SqliteStore
from astrocyte_sqlite import store as store_mod


def _item(id_: str, vec: list[float], text: str = "t", bank: str = "b", **kw) -> VectorItem:
    return VectorItem(id=id_, bank_id=bank, vector=vec, text=text, **kw)


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "mem.db")


# ── persistence & configuration ──────────────────────────────────────────


async def test_memories_survive_a_new_store_instance(db):
    await SqliteStore(path=db).store_vectors([_item("a", [1.0, 0.0], "we chose sqlite")])
    reopened = SqliteStore(path=db)
    hits = await reopened.search_similar([1.0, 0.0], "b")
    assert [h.id for h in hits] == ["a"]
    assert [h.document_id for h in await reopened.search_fulltext("sqlite", "b")] == ["a"]


async def test_parent_directories_are_created(tmp_path):
    path = tmp_path / "deep" / "er" / "mem.db"
    await SqliteStore(path=str(path)).store_vectors([_item("a", [1.0])])
    assert path.exists()


def test_path_defaults_to_env_then_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTROCYTE_SQLITE_PATH", str(tmp_path / "env.db"))
    assert SqliteStore().path == str(tmp_path / "env.db")
    monkeypatch.delenv("ASTROCYTE_SQLITE_PATH")
    assert SqliteStore().path.endswith(".local/share/astrocyte/astrocyte.db")


def test_memory_path_is_rejected_with_guidance():
    with pytest.raises(ValueError, match="in_memory store"):
        SqliteStore(path=":memory:")


# ── embedding dimension pinning ──────────────────────────────────────────


async def test_first_write_pins_the_dimension(db):
    s = SqliteStore(path=db)
    await s.store_vectors([_item("a", [1.0, 0.0, 0.0])])
    with pytest.raises(ValueError, match="embedding_dimensions 3"):
        await s.store_vectors([_item("b", [1.0, 0.0])])
    with pytest.raises(ValueError, match="embedding_dimensions 3"):
        await s.search_similar([1.0, 0.0], "b")


async def test_pinned_dimension_is_enforced_for_a_fresh_instance(db):
    """A different process (fresh instance) must also see the pinned dim."""
    await SqliteStore(path=db).store_vectors([_item("a", [1.0, 0.0, 0.0])])
    with pytest.raises(ValueError):
        await SqliteStore(path=db).store_vectors([_item("b", [1.0, 0.0])])


async def test_configured_dimension_conflicting_with_file_fails_loudly(db):
    """Switching embedding models against an existing file must not silently
    mix incomparable vectors."""
    await SqliteStore(path=db).store_vectors([_item("a", [1.0, 0.0, 0.0])])
    conflicting = SqliteStore(path=db, embedding_dimensions=384)
    with pytest.raises(ValueError, match="holds 3-dim embeddings"):
        await conflicting.search_similar([0.0] * 384, "b")
    # health() reports the same conflict rather than raising.
    status = await SqliteStore(path=db, embedding_dimensions=384).health()
    assert status.healthy is False and "holds 3-dim embeddings" in status.message


# ── time handling ────────────────────────────────────────────────────────


async def test_timestamps_round_trip_to_the_microsecond(db):
    when = datetime(2026, 3, 4, 5, 6, 7, 891_011, tzinfo=UTC)
    s = SqliteStore(path=db)
    await s.store_vectors([_item("a", [1.0], occurred_at=when, retained_at=when)])
    [item] = await s.list_vectors("b")
    assert item.occurred_at == when and item.retained_at == when


async def test_naive_and_offset_datetimes_are_normalised_to_utc(db):
    naive = datetime(2026, 1, 1, 12, 0)
    plus8 = datetime(2026, 1, 1, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    s = SqliteStore(path=db)
    await s.store_vectors([_item("n", [1.0], occurred_at=naive), _item("p", [1.0], occurred_at=plus8)])
    got = {i.id: i.occurred_at for i in await s.list_vectors("b")}
    assert got["n"] == got["p"] == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


async def test_as_of_time_travel_sees_a_memory_before_it_was_forgotten(db):
    s = SqliteStore(path=db)
    stored = datetime(2026, 1, 1, tzinfo=UTC)
    await s.store_vectors([_item("a", [1.0], retained_at=stored)])
    await s.delete(["a"], "b")
    assert await s.search_similar([1.0], "b") == []
    past = await s.search_similar([1.0], "b", filters=VectorFilters(as_of=stored + timedelta(days=1)))
    assert [h.id for h in past] == ["a"]


# ── keyword search ───────────────────────────────────────────────────────


async def test_editing_text_reindexes_it(db):
    s = SqliteStore(path=db)
    await s.store_vectors([_item("a", [1.0], "the cache lives in redis")])
    await s.store_vectors([_item("a", [1.0], "the cache moved to memcached")])
    assert await s.search_fulltext("redis", "b") == []
    assert [h.document_id for h in await s.search_fulltext("memcached", "b")] == ["a"]


async def test_forgotten_memories_leave_keyword_results(db):
    s = SqliteStore(path=db)
    await s.store_vectors([_item("a", [1.0], "deploys happen on tuesdays")])
    await s.delete(["a"], "b")
    assert await s.search_fulltext("deploys", "b") == []


@pytest.mark.parametrize(
    "query",
    [
        '"unbalanced',
        "NEAR(a b)",
        "foo*",
        "-bar",
        "a OR b",
        "(paren",
        "col:value",
        "^start",
        "x AND",
        "'; DROP TABLE x;--",
    ],
)
async def test_fts_syntax_in_user_queries_never_raises(db, query):
    s = SqliteStore(path=db)
    await s.store_vectors([_item("a", [1.0], "near foo bar value start paren")])
    await s.search_fulltext(query, "b")  # must not raise sqlite3.OperationalError


async def test_like_fallback_when_sqlite_lacks_fts5(db, monkeypatch):
    """Builds without FTS5 still get keyword recall: every non-stopword term
    must appear, ranked by density."""
    monkeypatch.setattr(store_mod, "_FTS_SCHEMA", "CREATE VIRTUAL TABLE x USING no_such_module(y);")
    s = SqliteStore(path=db)
    await s.store_vectors(
        [
            _item("a", [1.0], "the billing database runs postgres"),
            _item("b", [1.0], "the billing service"),
        ]
    )
    assert s.fts_enabled is False
    assert [h.document_id for h in await s.search_fulltext("what is the billing database", "b")] == ["a"]
    assert "LIKE fallback" in (await s.health()).message


# ── concurrency across processes ─────────────────────────────────────────


def _writer(path: str, worker: int, n: int) -> None:
    import asyncio

    async def go() -> None:
        s = SqliteStore(path=path)
        for i in range(n):
            await s.store_vectors([_item(f"w{worker}-{i}", [float(worker), float(i), 1.0], f"note {worker} {i}")])

    asyncio.run(go())


async def test_concurrent_writer_processes_lose_nothing(db):
    """The real deployment: an MCP server and several hook processes writing
    the same file at once. WAL + BEGIN IMMEDIATE + busy timeout must mean no
    'database is locked' errors and no lost writes."""
    await SqliteStore(path=db).store_vectors([_item("seed", [0.0, 0.0, 1.0])])
    ctx = mp.get_context("spawn")
    workers, per_worker = 6, 40
    procs = [ctx.Process(target=_writer, args=(db, w, per_worker)) for w in range(workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    assert [p.exitcode for p in procs] == [0] * workers
    items = await SqliteStore(path=db).list_vectors("b", limit=10_000)
    assert len(items) == workers * per_worker + 1


def test_journal_mode_is_wal(db):
    import asyncio

    asyncio.run(SqliteStore(path=db).health())
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


# ── health ───────────────────────────────────────────────────────────────


async def test_health_reports_path_and_keyword_mode(db):
    status = await SqliteStore(path=db).health()
    assert status.healthy and db in status.message and "fts5" in status.message


async def test_health_reports_an_unusable_path_instead_of_raising(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    status = await SqliteStore(path=str(blocker / "mem.db")).health()
    assert status.healthy is False


# ── erasing and enumerating (outside the SPI; used by `astrocyte memory`) ──


def _on_disk(db: str, word: bytes) -> bool:
    from pathlib import Path

    blob = b"".join(p.read_bytes() for p in Path(db).parent.glob(Path(db).name + "*"))
    return word in blob.lower()


async def test_purge_erases_forgotten_rows_from_every_database_file(db):
    """Soft delete keeps the text (for as_of); purge must leave no copy —
    not in the table, its free space, the full-text index or the WAL."""
    store = SqliteStore(path=db)
    await store.store_vectors([_item("a", [1.0, 0.0], "password Kestrel-Opal-Fjord"),
                               _item("b", [0.0, 1.0], "tuesday")])
    await store.delete(["a"], "b")
    assert _on_disk(db, b"kestrel"), "soft delete keeps the text"
    assert await store.purge("b", ["a"]) == 1
    assert not _on_disk(db, b"kestrel") and not _on_disk(db, b"fjord")
    assert [h.id for h in await store.search_similar([0.0, 1.0], "b")] == ["b"]


async def test_purge_only_touches_forgotten_rows_of_that_bank(db):
    store = SqliteStore(path=db)
    await store.store_vectors([_item("live", [1.0, 0.0]), _item("gone", [1.0, 0.0]),
                               _item("other", [1.0, 0.0], bank="c")])
    await store.delete(["gone"], "b")
    await store.delete(["other"], "c")
    assert await store.purge("b", ["live"]) == 0, "purge never bypasses forget"
    assert await store.purge("b") == 1
    assert [i.id for i in await store.list_vectors("b")] == ["live"]
    assert await store.purge("c") == 1


async def test_list_banks_counts_live_memories(db):
    store = SqliteStore(path=db)
    t = datetime(2026, 10, 1, tzinfo=UTC)
    await store.store_vectors([_item("1", [1.0], occurred_at=t), _item("2", [1.0]), _item("3", [1.0], bank="c")])
    await store.delete(["2"], "b")
    banks = {b: (n, newest) for b, n, newest in await store.list_banks()}
    assert banks["b"] == (1, t) and banks["c"][0] == 1


# ── filters and lookups, without Postgres ────────────────────────────────
# The parity suite checks these against PostgresStore; these pin the
# behaviour on machines without a database.


async def _seeded(db) -> SqliteStore:
    store = SqliteStore(path=db)
    jan, feb = datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 2, 15, tzinfo=UTC)
    await store.store_vectors([
        _item("w", [1.0, 0.0], "deploys on tuesday", tags=["ops"], fact_type="world", occurred_at=jan, chunk_id="c1"),
        _item("e", [0.9, 0.1], "deploys broke friday", tags=["ops", "incident"], fact_type="experience",
              occurred_at=feb, chunk_id="c2"),
        _item("u", [0.8, 0.2], "untagged note about deploys"),
    ])
    return store


async def test_tag_and_fact_type_filters(db):
    store = await _seeded(db)
    tagged = await store.search_similar([1.0, 0.0], "b", filters=VectorFilters(tags=["incident"]))
    assert [h.id for h in tagged] == ["e"], "untagged memories never match a tag filter"
    typed = await store.search_similar([1.0, 0.0], "b", filters=VectorFilters(fact_types=["world"]))
    assert [h.id for h in typed] == ["w"]


async def test_time_range_applies_to_recency_listing(db):
    store = await _seeded(db)
    feb = (datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 2, 28, tzinfo=UTC))
    assert [i.id for i in await store.list_recent_vectors("b", filters=VectorFilters(time_range=feb))] == ["e"]


async def test_as_of_sees_only_what_existed_then(db):
    store = await _seeded(db)
    before = datetime.now(UTC) - timedelta(days=1)
    assert await store.search_similar([1.0, 0.0], "b", filters=VectorFilters(as_of=before)) == []


async def test_keyword_search_edge_cases(db):
    from astrocyte.types import DocumentFilters

    store = await _seeded(db)
    assert await store.search_fulltext("   ", "b") == []
    assert await store.search_fulltext("the and of", "b") == [], "all stopwords: an empty query, as in Postgres"
    hits = await store.search_fulltext("deploys", "b", filters=DocumentFilters(tags=["incident"]))
    assert [h.document_id for h in hits] == ["e"]


async def test_chunk_and_document_lookups(db):
    store = await _seeded(db)
    assert [h.id for h in await store.get_by_chunk_ids(["c2", "nope"], "b")] == ["e"]
    assert await store.get_by_chunk_ids([], "b") == []
    doc = await store.get_document("w", "b")
    assert doc.text == "deploys on tuesday" and doc.tags == ["ops"]
    await store.delete(["w"], "b")
    assert await store.get_document("w", "b") is None


async def test_degenerate_queries_return_nothing(db):
    store = await _seeded(db)
    assert await store.search_similar([1.0, 0.0], "b", limit=0) == []
    assert await store.store_vectors([]) == []
    assert await store.delete([], "b") == 0
    assert await store.purge("b", []) == 0
    zero = await store.search_similar([0.0, 0.0], "b")
    assert all(h.score == 0.0 for h in zero), "a zero query vector ranks nothing above anything"


async def test_a_failing_batch_writes_nothing(db):
    store = await _seeded(db)
    with pytest.raises(ValueError):
        await store.store_vectors([_item("ok", [1.0, 0.0]), _item("bad", [1.0, 0.0, 0.0])])
    assert await store.get_document("ok", "b") is None, "batches are atomic"


async def test_a_dimension_recorded_by_another_process_is_enforced(db):
    await SqliteStore(path=db).store_vectors([_item("a", [1.0, 0.0])])
    fresh = SqliteStore(path=db)
    fresh._dim = None  # as if it opened before the other process's first write
    with pytest.raises(ValueError, match="embedding_dimensions"):
        await fresh.store_vectors([_item("b", [1.0, 0.0, 0.0])])


# ── privacy ──────────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
async def test_a_new_store_is_private_to_its_owner(tmp_path):
    """Memories hold conversations: other accounts must not read them."""
    db = tmp_path / "fresh" / "mem.db"
    await SqliteStore(path=str(db)).store_vectors([_item("a", [1.0, 0.0], "said in passing")])
    assert oct(db.parent.stat().st_mode & 0o777) == "0o700"
    for f in db.parent.iterdir():  # the database and its -wal / -shm files
        assert oct(f.stat().st_mode & 0o777) == "0o600", f.name


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
async def test_an_existing_store_keeps_its_permissions(tmp_path):
    db = tmp_path / "mem.db"
    await SqliteStore(path=str(db)).store_vectors([_item("a", [1.0, 0.0])])
    db.chmod(0o640)  # an operator's deliberate choice
    await SqliteStore(path=str(db)).store_vectors([_item("b", [0.0, 1.0])])
    assert oct(db.stat().st_mode & 0o777) == "0o640"
