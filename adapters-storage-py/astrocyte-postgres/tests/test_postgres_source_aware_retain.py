"""Postgres integration test for source-aware retain (M10).

Pins the cross-adapter contract: PostgresStore.store_vectors must
persist ``chunk_id`` and search_similar must return it. The InMemory
parity is covered by ``astrocyte-py/tests/test_source_aware_retain.py``;
this test makes sure the Postgres adapter doesn't silently drop the
backreference column.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from astrocyte.types import VectorItem

from astrocyte_postgres.source_store import PostgresSourceStore
from astrocyte_postgres.store import PostgresStore

DIM = 3


@pytest.fixture
def dsn() -> str:
    import os

    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    return url


@pytest.fixture
async def stores(dsn: str):
    """PostgresStore + PostgresSourceStore on a private table; teardown drops both."""
    table = f"test_sar_{uuid.uuid4().hex[:8]}"
    vs = PostgresStore(dsn=dsn, table_name=table, embedding_dimensions=DIM)
    ss = PostgresSourceStore(dsn=dsn)
    yield vs, ss
    conn = await psycopg.AsyncConnection.connect(dsn)
    async with conn:
        await conn.execute(f"DROP TABLE IF EXISTS {table}")
        # Best-effort cleanup of source rows from this test run.
        await conn.execute(
            "DELETE FROM astrocyte_source_chunks WHERE bank_id LIKE 'sar-%%'"
        )
        await conn.execute(
            "DELETE FROM astrocyte_source_documents WHERE bank_id LIKE 'sar-%%'"
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_chunk_id_round_trips_through_postgres_store(stores) -> None:
    """End-to-end: source_store creates chunk → vector store stores it
    with chunk_id → search_similar returns the chunk_id."""
    from astrocyte.types import SourceChunk, SourceDocument

    vs, ss = stores
    bank = f"sar-{uuid.uuid4().hex[:6]}"

    # 1. Create a SourceDocument + 2 chunks.
    doc_id = await ss.store_document(SourceDocument(
        id=f"doc-{uuid.uuid4().hex[:6]}",
        bank_id=bank,
        content_hash=uuid.uuid4().hex,
    ))
    chunk_ids = await ss.store_chunks([
        SourceChunk(id=f"{doc_id}:0", bank_id=bank, document_id=doc_id,
                    chunk_index=0, text="first chunk text"),
        SourceChunk(id=f"{doc_id}:1", bank_id=bank, document_id=doc_id,
                    chunk_index=1, text="second chunk text"),
    ])
    assert len(chunk_ids) == 2

    # 2. Store vectors with chunk_id stamped on each item.
    await vs.store_vectors([
        VectorItem(id="m1", bank_id=bank, vector=[1.0, 0.0, 0.0],
                   text="first chunk text", chunk_id=chunk_ids[0]),
        VectorItem(id="m2", bank_id=bank, vector=[0.0, 1.0, 0.0],
                   text="second chunk text", chunk_id=chunk_ids[1]),
    ])

    # 3. search_similar must surface chunk_id on every hit.
    hits = await vs.search_similar([1.0, 0.0, 0.0], bank, limit=5)
    assert len(hits) == 2
    by_id = {h.id: h for h in hits}
    assert by_id["m1"].chunk_id == chunk_ids[0]
    assert by_id["m2"].chunk_id == chunk_ids[1]


@pytest.mark.asyncio
async def test_get_by_chunk_ids_returns_only_matching_vectors(stores) -> None:
    """``get_by_chunk_ids`` must filter by both bank and chunk_id list."""
    vs, _ss = stores
    bank = f"sar-{uuid.uuid4().hex[:6]}"

    await vs.store_vectors([
        VectorItem(id="m1", bank_id=bank, vector=[1.0, 0.0, 0.0],
                   text="t1", chunk_id="c1"),
        VectorItem(id="m2", bank_id=bank, vector=[0.0, 1.0, 0.0],
                   text="t2", chunk_id="c2"),
        VectorItem(id="m3", bank_id=bank, vector=[0.0, 0.0, 1.0],
                   text="t3", chunk_id=None),  # no provenance — must be excluded
    ])

    hits = await vs.get_by_chunk_ids(["c1", "c2", "missing"], bank)
    assert {h.id for h in hits} == {"m1", "m2"}
    # Score is set to 1.0 by the adapter; the orchestrator applies the
    # multiplier downstream.
    for h in hits:
        assert h.score == pytest.approx(1.0)

    # Empty input is a fast path that returns [].
    assert await vs.get_by_chunk_ids([], bank) == []


@pytest.mark.asyncio
async def test_get_by_chunk_ids_isolates_by_bank(stores) -> None:
    """Chunk ids in a different bank must not leak across the boundary."""
    vs, _ss = stores
    bank_a = f"sar-{uuid.uuid4().hex[:6]}"
    bank_b = f"sar-{uuid.uuid4().hex[:6]}"

    await vs.store_vectors([
        VectorItem(id="ma", bank_id=bank_a, vector=[1.0, 0.0, 0.0],
                   text="a", chunk_id="shared-cid"),
        VectorItem(id="mb", bank_id=bank_b, vector=[1.0, 0.0, 0.0],
                   text="b", chunk_id="shared-cid"),
    ])

    a_hits = await vs.get_by_chunk_ids(["shared-cid"], bank_a)
    b_hits = await vs.get_by_chunk_ids(["shared-cid"], bank_b)
    assert {h.id for h in a_hits} == {"ma"}
    assert {h.id for h in b_hits} == {"mb"}
