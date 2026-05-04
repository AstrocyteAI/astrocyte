"""Integration tests for ``PostgresSourceStore`` (M10).

Hits a live PostgreSQL via ``DATABASE_URL`` (skipped if unset). Each test
uses a unique ``bank_id`` so they don't collide across parallel runs and
the per-test teardown removes only that bank's rows.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from astrocyte.types import SourceChunk, SourceDocument

from astrocyte_postgres.source_store import PostgresSourceStore


@pytest.fixture
def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — skipping postgres source-store tests")
    return url


@pytest.fixture
async def store(dsn: str):
    """Returns (store, bank_id); tears down all rows for that bank."""
    bank = f"src-bank-{uuid.uuid4().hex[:8]}"
    s = PostgresSourceStore(dsn=dsn, bootstrap_schema=False)
    yield s, bank
    # Hard-delete all rows for this test's bank (chunks cascade).
    conn = await psycopg.AsyncConnection.connect(dsn)
    async with conn:
        await conn.execute(
            "DELETE FROM public.astrocyte_source_documents WHERE bank_id = %s", [bank],
        )
        await conn.commit()


def _doc(
    doc_id: str,
    bank: str,
    *,
    content_hash: str | None = None,
    title: str | None = None,
) -> SourceDocument:
    return SourceDocument(
        id=doc_id,
        bank_id=bank,
        title=title or f"doc {doc_id}",
        source_uri=f"memory://{doc_id}",
        content_hash=content_hash,
        content_type="text/plain",
        metadata={"k": "v"},
    )


def _chunk(
    chunk_id: str,
    document_id: str,
    bank: str,
    *,
    chunk_index: int = 0,
    content_hash: str | None = None,
    text: str | None = None,
) -> SourceChunk:
    return SourceChunk(
        id=chunk_id,
        bank_id=bank,
        document_id=document_id,
        chunk_index=chunk_index,
        text=text or f"text-{chunk_id}",
        content_hash=content_hash,
        metadata={},
    )


class TestDocumentCRUD:
    @pytest.mark.asyncio
    async def test_store_and_get_document(self, store):
        s, bank = store
        sid = await s.store_document(_doc("d1", bank))
        assert sid == "d1"
        got = await s.get_document("d1", bank)
        assert got is not None
        assert got.id == "d1"
        assert got.title == "doc d1"
        assert got.metadata == {"k": "v"}

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self, store):
        s, bank = store
        assert await s.get_document("never", bank) is None

    @pytest.mark.asyncio
    async def test_list_documents_orders_recent_first(self, store):
        s, bank = store
        await s.store_document(_doc("oldest", bank))
        await s.store_document(_doc("middle", bank))
        await s.store_document(_doc("newest", bank))
        listed = await s.list_documents(bank)
        # Most-recently-stored first (matches the bank_created_idx).
        assert listed[0].id == "newest"
        assert {d.id for d in listed} == {"oldest", "middle", "newest"}


class TestDocumentDedup:
    @pytest.mark.asyncio
    async def test_store_dedups_on_content_hash(self, store):
        s, bank = store
        first = await s.store_document(_doc("d-orig", bank, content_hash="hash-A"))
        second = await s.store_document(_doc("d-DIFFERENT", bank, content_hash="hash-A"))
        assert first == "d-orig"
        # Same hash → existing id wins, NEW id is not created.
        assert second == "d-orig"

    @pytest.mark.asyncio
    async def test_no_hash_skips_dedup(self, store):
        s, bank = store
        a = await s.store_document(_doc("d1", bank))
        b = await s.store_document(_doc("d2", bank))
        assert a == "d1"
        assert b == "d2"

    @pytest.mark.asyncio
    async def test_find_document_by_hash(self, store):
        s, bank = store
        await s.store_document(_doc("d1", bank, content_hash="hash-X"))
        found = await s.find_document_by_hash("hash-X", bank)
        assert found is not None
        assert found.id == "d1"

    @pytest.mark.asyncio
    async def test_find_document_by_hash_returns_none_for_missing(self, store):
        s, bank = store
        assert await s.find_document_by_hash("no-such", bank) is None


class TestSoftDeleteDocument:
    @pytest.mark.asyncio
    async def test_delete_returns_true_for_existing(self, store):
        s, bank = store
        await s.store_document(_doc("d1", bank))
        deleted = await s.delete_document("d1", bank)
        assert deleted is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_for_missing(self, store):
        s, bank = store
        deleted = await s.delete_document("never", bank)
        assert deleted is False

    @pytest.mark.asyncio
    async def test_get_returns_none_after_delete(self, store):
        s, bank = store
        await s.store_document(_doc("d1", bank))
        await s.delete_document("d1", bank)
        assert await s.get_document("d1", bank) is None

    @pytest.mark.asyncio
    async def test_list_omits_deleted(self, store):
        s, bank = store
        await s.store_document(_doc("kept", bank))
        await s.store_document(_doc("dropped", bank))
        await s.delete_document("dropped", bank)
        listed = await s.list_documents(bank)
        assert {d.id for d in listed} == {"kept"}

    @pytest.mark.asyncio
    async def test_dedup_probe_skips_soft_deleted(self, store):
        """A new doc with the same hash as a soft-deleted one is allowed —
        the dedup probe filters ``deleted_at IS NULL``."""
        s, bank = store
        await s.store_document(_doc("d-old", bank, content_hash="h"))
        await s.delete_document("d-old", bank)
        new_id = await s.store_document(_doc("d-new", bank, content_hash="h"))
        # Different id wins (no live row blocked the insert).
        assert new_id == "d-new"


class TestChunkCRUD:
    @pytest.mark.asyncio
    async def test_store_chunks_returns_ids_in_order(self, store):
        s, bank = store
        await s.store_document(_doc("d1", bank))
        ids = await s.store_chunks([
            _chunk(f"c{i}", "d1", bank, chunk_index=i, content_hash=f"h{i}")
            for i in range(3)
        ])
        assert ids == ["c0", "c1", "c2"]

    @pytest.mark.asyncio
    async def test_list_chunks_orders_by_index(self, store):
        s, bank = store
        await s.store_document(_doc("d1", bank))
        # Insert out of order.
        await s.store_chunks([
            _chunk("c2", "d1", bank, chunk_index=2),
            _chunk("c0", "d1", bank, chunk_index=0),
            _chunk("c1", "d1", bank, chunk_index=1),
        ])
        listed = await s.list_chunks("d1", bank)
        assert [c.chunk_index for c in listed] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_get_chunk(self, store):
        s, bank = store
        await s.store_document(_doc("d1", bank))
        await s.store_chunks([_chunk("c0", "d1", bank)])
        got = await s.get_chunk("c0", bank)
        assert got is not None
        assert got.id == "c0"

    @pytest.mark.asyncio
    async def test_get_chunk_returns_none_for_missing(self, store):
        s, bank = store
        assert await s.get_chunk("never", bank) is None

    @pytest.mark.asyncio
    async def test_store_empty_chunks_returns_empty_list(self, store):
        s, _ = store
        assert await s.store_chunks([]) == []


class TestChunkDedup:
    @pytest.mark.asyncio
    async def test_chunk_dedup_returns_existing_id(self, store):
        s, bank = store
        await s.store_document(_doc("d1", bank))
        first = await s.store_chunks([_chunk("c-orig", "d1", bank, content_hash="h0")])
        second = await s.store_chunks([
            _chunk("c-DIFFERENT", "d1", bank, chunk_index=99, content_hash="h0"),
        ])
        assert first == ["c-orig"]
        assert second == ["c-orig"]

    @pytest.mark.asyncio
    async def test_batched_dedup_in_single_call(self, store):
        """A single store_chunks call mixing new + duplicate hashes returns
        existing ids for dups and inserts the new ones — using the batched
        single-query probe path."""
        s, bank = store
        await s.store_document(_doc("d1", bank))
        # Seed an existing chunk with hash 'h0'.
        await s.store_chunks([_chunk("c-existing", "d1", bank, content_hash="h0")])
        # Now mix: 1 dup of h0 + 2 new chunks.
        ids = await s.store_chunks([
            _chunk("c-dup", "d1", bank, chunk_index=10, content_hash="h0"),
            _chunk("c-new1", "d1", bank, chunk_index=11, content_hash="h1"),
            _chunk("c-new2", "d1", bank, chunk_index=12, content_hash="h2"),
        ])
        assert ids == ["c-existing", "c-new1", "c-new2"]

    @pytest.mark.asyncio
    async def test_find_chunk_by_hash(self, store):
        s, bank = store
        await s.store_document(_doc("d1", bank))
        await s.store_chunks([_chunk("c0", "d1", bank, content_hash="hash-Y")])
        found = await s.find_chunk_by_hash("hash-Y", bank)
        assert found is not None
        assert found.id == "c0"

    @pytest.mark.asyncio
    async def test_find_chunk_by_hash_returns_none_for_missing(self, store):
        s, bank = store
        assert await s.find_chunk_by_hash("no-such", bank) is None


class TestCascadeDelete:
    """Schema FK is ``ON DELETE CASCADE``; verify hard-deleting the parent
    document removes its chunks. Soft-delete (the default) leaves chunks
    in place so re-extraction flows can still resolve provenance."""

    @pytest.mark.asyncio
    async def test_hard_delete_document_cascades_to_chunks(self, store, dsn):
        s, bank = store
        await s.store_document(_doc("d1", bank))
        await s.store_chunks([
            _chunk("c0", "d1", bank, chunk_index=0),
            _chunk("c1", "d1", bank, chunk_index=1),
        ])

        # Hard-delete the doc directly to exercise the FK CASCADE.
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            await conn.execute(
                "DELETE FROM public.astrocyte_source_documents WHERE bank_id=%s AND id=%s",
                [bank, "d1"],
            )
            await conn.commit()

        # Chunks must be gone.
        listed = await s.list_chunks("d1", bank)
        assert listed == []

    @pytest.mark.asyncio
    async def test_soft_delete_document_does_not_cascade(self, store):
        """Soft-delete is just an ``UPDATE deleted_at = NOW()``; chunks
        stay queryable so re-extraction can still resolve provenance."""
        s, bank = store
        await s.store_document(_doc("d1", bank))
        await s.store_chunks([_chunk("c0", "d1", bank, chunk_index=0)])
        await s.delete_document("d1", bank)
        # Chunks are still there (no cascade on soft-delete).
        listed = await s.list_chunks("d1", bank)
        assert len(listed) == 1


class TestHealth:
    @pytest.mark.asyncio
    async def test_healthy_when_db_reachable(self, store):
        s, _ = store
        h = await s.health()
        assert h.healthy is True
