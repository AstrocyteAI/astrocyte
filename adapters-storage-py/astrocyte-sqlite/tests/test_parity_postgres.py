"""Differential parity: SqliteStore must behave like PostgresStore.

Postgres is the production backend and what every benchmark measures, so it
is the contract. Each test drives identical operations against both stores and
fails on any divergence in results, ordering, scores, or error behaviour.

Requires a pgvector Postgres. Keyed on a dedicated variable rather than
DATABASE_URL, because Doppler injects its own DATABASE_URL pointing at the
bench database, and a parity run must never touch that::

    docker run -d --name astrocyte-sqlite-parity -e POSTGRES_PASSWORD=parity \\
        -e POSTGRES_DB=parity -p 55499:5432 pgvector/pgvector:pg16
    ASTROCYTE_PARITY_PG_DSN=postgresql://postgres:parity@localhost:55499/parity \\
        uv run --with-editable ../astrocyte-postgres pytest tests/test_parity_postgres.py
"""

from __future__ import annotations

import os
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from astrocyte.types import DocumentFilters, VectorFilters, VectorItem

from astrocyte_sqlite import SqliteStore

psycopg = pytest.importorskip("psycopg")
astrocyte_postgres = pytest.importorskip("astrocyte_postgres")

DIM = 8
BANKS = ("bank-a", "bank-b")
TAG_POOL = ("db", "api", "ui", "auth", "infra")
FACT_TYPES = ("world", "experience", "observation")
SCORE_TOL = 1e-5


@pytest.fixture
async def pg_dsn() -> str:
    dsn = os.environ.get("ASTROCYTE_PARITY_PG_DSN")
    if not dsn:
        pytest.skip("ASTROCYTE_PARITY_PG_DSN not set — skipping Postgres parity")
    # As in a deployment, where migrations create the extension first. On a
    # fresh database PostgresStore's own bootstrap opens its pool before
    # creating it, so its first connections never register the vector type
    # and return embeddings as text (PostgresStore now decodes that form;
    # see astrocyte_postgres._vectors). This suite compares semantics, not
    # that race.
    conn = await psycopg.AsyncConnection.connect(dsn)
    async with conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.commit()
    return dsn


@pytest.fixture
async def stores(pg_dsn: str, tmp_path):
    table = f"parity_{uuid.uuid4().hex[:12]}"
    pg = astrocyte_postgres.PostgresStore(dsn=pg_dsn, table_name=table, embedding_dimensions=DIM)
    lite = SqliteStore(path=str(tmp_path / "parity.db"), embedding_dimensions=DIM)
    yield pg, lite
    await pg.close()
    conn = await psycopg.AsyncConnection.connect(pg_dsn)
    async with conn:
        await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        await conn.commit()


# ── generators ───────────────────────────────────────────────────────────


def _when(rng: random.Random) -> datetime:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return base + timedelta(days=rng.randint(0, 200), microseconds=rng.randint(0, 999_999))


def _vector(rng: random.Random) -> list[float]:
    # Mixed signs so negative cosines occur and clamping is exercised.
    return [rng.uniform(-1, 1) for _ in range(DIM)]


def _item(rng: random.Random, item_id: str | None = None) -> VectorItem:
    tags = rng.choice([None, [], rng.sample(TAG_POOL, rng.randint(1, 3))])
    return VectorItem(
        id=item_id or str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        bank_id=rng.choice(BANKS),
        vector=_vector(rng),
        text=f"memory {rng.randint(0, 10**6)}",
        metadata=rng.choice([None, {"session_id": rng.choice(["s1", "s2"]), "n": rng.randint(0, 9)}]),
        tags=tags,
        fact_type=rng.choice([None, *FACT_TYPES]),
        occurred_at=rng.choice([None, _when(rng)]),
        memory_layer=rng.choice([None, "fact", "observation"]),
        retained_at=_when(rng),
        chunk_id=rng.choice([None, "c1", "c2", "c3"]),
    )


def _filters(rng: random.Random) -> VectorFilters | None:
    if rng.random() < 0.25:
        return None
    start = _when(rng)
    return VectorFilters(
        tags=rng.choice([None, rng.sample(TAG_POOL, rng.randint(1, 2))]),
        fact_types=rng.choice([None, rng.sample(FACT_TYPES, rng.randint(1, 2))]),
        as_of=rng.choice([None, None, _when(rng), datetime.now(UTC) + timedelta(days=1)]),
        time_range=rng.choice([None, (start, start + timedelta(days=60))]),
        session_id=rng.choice([None, "s1"]),
    )


# ── normalisation ────────────────────────────────────────────────────────


def _hits(hits: list[Any]) -> list[tuple]:
    return [
        (h.id, h.text, h.metadata, h.tags, h.fact_type, h.occurred_at, h.memory_layer, h.retained_at, h.chunk_id)
        for h in hits
    ]


def _items(items: list[VectorItem]) -> list[tuple]:
    return [
        (i.id, i.bank_id, i.text, i.metadata, i.tags, i.fact_type, i.occurred_at, i.memory_layer, i.retained_at)
        for i in items
    ]


def _assert_vectors_close(a: list[VectorItem], b: list[VectorItem]) -> None:
    for x, y in zip(a, b, strict=True):
        assert x.vector == pytest.approx(y.vector, abs=1e-6), x.id


def _assert_ranked_equal(pg_hits: list[Any], lite_hits: list[Any]) -> None:
    assert [h.id for h in lite_hits] == [h.id for h in pg_hits]
    assert [h.score for h in lite_hits] == pytest.approx([h.score for h in pg_hits], abs=SCORE_TOL)
    assert _hits(lite_hits) == _hits(pg_hits)


async def _seed(stores, rng: random.Random, n: int) -> list[VectorItem]:
    pg, lite = stores
    items = [_item(rng) for _ in range(n)]
    for batch_start in range(0, n, 7):
        batch = items[batch_start : batch_start + 7]
        assert await pg.store_vectors(batch) == await lite.store_vectors(batch)
    return items


# ── vector-store parity ──────────────────────────────────────────────────


# ASTROCYTE_PARITY_SEEDS widens the sweep for a deeper local soak.
@pytest.mark.parametrize("seed", range(int(os.environ.get("ASTROCYTE_PARITY_SEEDS", "6"))))
async def test_randomised_operation_sequences_match(stores, seed):
    """Seed, delete, resurrect, then query every read path with random filters."""
    rng = random.Random(seed)
    pg, lite = stores
    items = await _seed(stores, rng, 60)

    for _ in range(4):
        victims = rng.sample(items, 8)
        for bank in BANKS:
            ids = [v.id for v in victims] + ["missing-id"]
            assert await pg.delete(ids, bank) == await lite.delete(ids, bank)

    resurrected = [_item(rng, item_id=v.id) for v in rng.sample(items, 5)]
    assert await pg.store_vectors(resurrected) == await lite.store_vectors(resurrected)

    for _ in range(25):
        bank, limit, filters, q = rng.choice(BANKS), rng.randint(1, 30), _filters(rng), _vector(rng)
        _assert_ranked_equal(
            await pg.search_similar(q, bank, limit, filters),
            await lite.search_similar(q, bank, limit, filters),
        )
        pg_recent = await pg.list_recent_vectors(bank, limit, filters)
        lite_recent = await lite.list_recent_vectors(bank, limit, filters)
        assert _items(lite_recent) == _items(pg_recent)
        _assert_vectors_close(lite_recent, pg_recent)

    for bank in BANKS:
        for offset, limit in [(0, 100), (3, 5), (10, 1), (500, 10)]:
            pg_list = await pg.list_vectors(bank, offset, limit)
            lite_list = await lite.list_vectors(bank, offset, limit)
            assert _items(lite_list) == _items(pg_list)
            _assert_vectors_close(lite_list, pg_list)
        for chunks in (["c1"], ["c2", "c3"], ["nope"], []):
            assert _hits(await lite.get_by_chunk_ids(chunks, bank)) == sorted(
                _hits(await pg.get_by_chunk_ids(chunks, bank))
            )
        for probe in rng.sample(items, 10):
            assert await lite.get_document(probe.id, bank) == await pg.get_document(probe.id, bank)


def _changes(changes: list[Any]) -> list[tuple]:
    """Change-feed entries, compared exactly — except a tombstone's
    ``changed_at``, which is each store's own clock at forget time."""
    return [
        (c.id, c.bank_id, True, None)
        if c.deleted
        else (
            c.id,
            c.bank_id,
            False,
            (c.changed_at, c.text, c.metadata, c.tags, c.fact_type, c.occurred_at, c.memory_layer, c.retained_at),
        )
        for c in changes
    ]


async def _page_all(store, bank: str, limit: int, after=None) -> list[Any]:
    seen: list[Any] = []
    while page := await store.list_changes(bank, after=after, limit=limit):
        seen += page
        after = (page[-1].changed_at, page[-1].id)
    return seen


@pytest.mark.parametrize("seed", range(int(os.environ.get("ASTROCYTE_PARITY_SEEDS", "6"))))
async def test_change_feed_matches(stores, seed):
    """list_changes: same entries, same (changed_at, id) order, same paging —
    including ties on changed_at broken by ids that collate differently under
    a locale than byte-wise (mixed case, ``_``, ``-``)."""
    rng = random.Random(1000 + seed)
    pg, lite = stores
    items = await _seed(stores, rng, 40)
    tie = _when(rng)
    tied = []
    for item_id in ("Zeta", "alpha", "_under", "-dash", "B-2", "b_1", "a1B2", "A1b2"):
        item = _item(rng, item_id=item_id)
        item.retained_at = tie
        tied.append(item)
    assert await pg.store_vectors(tied) == await lite.store_vectors(tied)
    items += tied

    for _ in range(3):
        victims = rng.sample(items, 6)
        for bank in BANKS:
            ids = [v.id for v in victims]
            assert await pg.delete(ids, bank) == await lite.delete(ids, bank)
    resurrected = [_item(rng, item_id=v.id) for v in rng.sample(items, 4)]
    assert await pg.store_vectors(resurrected) == await lite.store_vectors(resurrected)

    for bank in BANKS:
        pg_all = await pg.list_changes(bank, limit=1000)
        lite_all = await lite.list_changes(bank, limit=1000)
        assert _changes(lite_all) == _changes(pg_all)
        assert len(pg_all) == len({c.id for c in pg_all})  # one entry per row
        for limit in (1, 3, 17):
            assert _changes(await _page_all(lite, bank, limit)) == _changes(pg_all)
            assert _changes(await _page_all(pg, bank, limit)) == _changes(pg_all)
        # Resuming from any live entry's position (identical in both stores).
        for start in rng.sample([c for c in pg_all if not c.deleted], 5):
            after = (start.changed_at, start.id)
            assert _changes(await lite.list_changes(bank, after=after, limit=7)) == _changes(
                await pg.list_changes(bank, after=after, limit=7)
            )


async def test_scores_are_clamped_identically(stores):
    pg, lite = stores
    # Explicit retained_at: each store would otherwise stamp its own "now".
    item = VectorItem(
        id="opposite",
        bank_id="b",
        vector=[1.0] + [0.0] * (DIM - 1),
        text="x",
        retained_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    await pg.store_vectors([item])
    await lite.store_vectors([item])
    query = [-1.0] + [0.0] * (DIM - 1)
    _assert_ranked_equal(await pg.search_similar(query, "b"), await lite.search_similar(query, "b"))
    assert (await lite.search_similar(query, "b"))[0].score == 0.0


async def test_bad_batch_is_rejected_atomically_by_both(stores):
    pg, lite = stores
    good = VectorItem(id="good", bank_id="b", vector=[0.1] * DIM, text="ok")
    bad = VectorItem(id="bad", bank_id="b", vector=[0.1] * (DIM + 1), text="wrong length")
    for store in (pg, lite):
        with pytest.raises(ValueError):
            await store.store_vectors([good, bad])
    assert await pg.list_vectors("b") == []
    assert await lite.list_vectors("b") == []


async def test_query_dimension_mismatch_raises_in_both(stores):
    pg, lite = stores
    for store in (pg, lite):
        await store.store_vectors([VectorItem(id="x", bank_id="b", vector=[0.1] * DIM, text="t")])
        with pytest.raises(ValueError):
            await store.search_similar([0.1] * (DIM + 2), "b")


# ── keyword (DocumentStore) parity ───────────────────────────────────────

CORPUS = {
    "k1": "We chose Postgres over Mongo for the billing database",
    "k2": "The deploy pipeline runs migrations before starting the API",
    "k3": "Authentication tokens rotate every thirty days",
    "k4": "Database migrations must be reversible",
    "k5": "The UI team prefers server-rendered pages",
    "k6": "Deploying on Fridays is discouraged by the infra team",
}

KEYWORD_QUERIES = [
    "database",
    "what is the database for billing",  # stopwords dropped, remaining ANDed
    "migrations",
    "migration",  # stemming: singular query finds plural text
    "deploy",
    "deploying pipeline",
    "token rotation",
    "the and of",  # all stopwords → no results
    "nonexistentterm",
    "UI team",
]


async def test_keyword_match_sets_match(stores):
    """Same documents match. Ranking legitimately differs (FTS5 bm25 vs
    ts_rank_cd); the pipeline fuses keyword hits by rank, so the match set is
    the contract."""
    pg, lite = stores
    rng = random.Random(7)
    items = []
    for doc_id, text in CORPUS.items():
        item = _item(rng, item_id=doc_id)
        item.bank_id, item.text, item.tags = "kb", text, ["db"] if "atabase" in text else ["other"]
        items.append(item)
    await pg.store_vectors(items)
    await lite.store_vectors(items)
    await pg.delete(["k6"], "kb")
    await lite.delete(["k6"], "kb")

    for query in KEYWORD_QUERIES:
        for filters in (None, DocumentFilters(tags=["db"])):
            pg_ids = {h.document_id for h in await pg.search_fulltext(query, "kb", 10, filters)}
            lite_ids = {h.document_id for h in await lite.search_fulltext(query, "kb", 10, filters)}
            assert lite_ids == pg_ids, f"{query!r} filters={filters}"


async def test_stopword_list_matches_postgres_english_config(pg_dsn):
    """Every word we drop must be one plainto_tsquery('english', …) drops."""
    from astrocyte_sqlite.store import _ENGLISH_STOPWORDS

    conn = await psycopg.AsyncConnection.connect(pg_dsn)
    async with conn:
        cur = await conn.execute(
            "SELECT w FROM unnest(%s::text[]) AS w WHERE plainto_tsquery('english', w)::text <> ''",
            (sorted(_ENGLISH_STOPWORDS),),
        )
        kept_by_postgres = [r[0] for r in await cur.fetchall()]
    assert kept_by_postgres == []
