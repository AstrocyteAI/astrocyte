"""Unit tests for ``InMemorySourceStore`` (the M10 reference implementation).

These pin the SourceStore SPI contract at the in-memory level so adapter
implementations (PostgresSourceStore et al.) have a single behavioural
spec to match. Postgres-specific behaviour is covered in
``adapters-storage-py/astrocyte-postgres/tests/test_postgres_source_store.py``.
"""

from __future__ import annotations

import pytest

from astrocyte.testing.in_memory import InMemorySourceStore
from astrocyte.types import SourceChunk, SourceDocument


def _doc(
    doc_id: str,
    *,
    bank_id: str = "bank-1",
    content_hash: str | None = None,
    title: str | None = None,
) -> SourceDocument:
    return SourceDocument(
        id=doc_id,
        bank_id=bank_id,
        title=title or f"doc {doc_id}",
        source_uri=f"memory://{doc_id}",
        content_hash=content_hash,
        content_type="text/plain",
        metadata={"k": "v"},
    )


def _chunk(
    chunk_id: str,
    document_id: str,
    *,
    bank_id: str = "bank-1",
    chunk_index: int = 0,
    content_hash: str | None = None,
    text: str | None = None,
) -> SourceChunk:
    return SourceChunk(
        id=chunk_id,
        bank_id=bank_id,
        document_id=document_id,
        chunk_index=chunk_index,
        text=text or f"text for {chunk_id}",
        content_hash=content_hash,
        metadata={},
    )


@pytest.fixture
def store() -> InMemorySourceStore:
    return InMemorySourceStore()


class TestDocumentCRUD:
    @pytest.mark.asyncio
    async def test_store_and_get_document(self, store):
        sid = await store.store_document(_doc("d1"))
        assert sid == "d1"
        got = await store.get_document("d1", "bank-1")
        assert got is not None
        assert got.id == "d1"
        assert got.title == "doc d1"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self, store):
        assert await store.get_document("nope", "bank-1") is None

    @pytest.mark.asyncio
    async def test_get_isolates_by_bank(self, store):
        """Same id in two banks must not collide."""
        await store.store_document(_doc("d1", bank_id="bank-1"))
        await store.store_document(_doc("d1", bank_id="bank-2", title="other"))
        a = await store.get_document("d1", "bank-1")
        b = await store.get_document("d1", "bank-2")
        assert a.title == "doc d1"
        assert b.title == "other"

    @pytest.mark.asyncio
    async def test_list_orders_recent_first(self, store):
        await store.store_document(_doc("oldest"))
        await store.store_document(_doc("middle"))
        await store.store_document(_doc("newest"))
        ids = [d.id for d in await store.list_documents("bank-1")]
        # InMemorySourceStore returns insertion order reversed (newest first).
        assert ids[0] == "newest"
        assert set(ids) == {"oldest", "middle", "newest"}


class TestDocumentDedup:
    """When ``content_hash`` is set, store_document is idempotent."""

    @pytest.mark.asyncio
    async def test_dedup_returns_existing_id(self, store):
        first = await store.store_document(_doc("d-first", content_hash="hash-A"))
        # Different requested id, same hash → existing id wins.
        second = await store.store_document(_doc("d-DIFFERENT", content_hash="hash-A"))
        assert first == "d-first"
        assert second == "d-first"

    @pytest.mark.asyncio
    async def test_dedup_isolates_by_bank(self, store):
        """Same hash in two banks does NOT dedup across banks."""
        a = await store.store_document(_doc("d-a", bank_id="bank-1", content_hash="h"))
        b = await store.store_document(_doc("d-b", bank_id="bank-2", content_hash="h"))
        assert a == "d-a"
        assert b == "d-b"

    @pytest.mark.asyncio
    async def test_no_hash_means_no_dedup(self, store):
        """Documents with content_hash=None always store a fresh row."""
        a = await store.store_document(_doc("d1"))
        b = await store.store_document(_doc("d2"))
        assert a == "d1"
        assert b == "d2"

    @pytest.mark.asyncio
    async def test_find_document_by_hash(self, store):
        await store.store_document(_doc("d1", content_hash="hash-X"))
        found = await store.find_document_by_hash("hash-X", "bank-1")
        assert found is not None
        assert found.id == "d1"

    @pytest.mark.asyncio
    async def test_find_document_by_hash_returns_none_for_missing(self, store):
        assert await store.find_document_by_hash("no-such", "bank-1") is None


class TestSoftDeleteDocument:
    @pytest.mark.asyncio
    async def test_delete_hides_from_get(self, store):
        await store.store_document(_doc("d1"))
        deleted = await store.delete_document("d1", "bank-1")
        assert deleted is True
        assert await store.get_document("d1", "bank-1") is None

    @pytest.mark.asyncio
    async def test_delete_returns_false_for_missing(self, store):
        assert await store.delete_document("nope", "bank-1") is False

    @pytest.mark.asyncio
    async def test_delete_cascades_to_chunks(self, store):
        await store.store_document(_doc("d1"))
        await store.store_chunks([_chunk(f"c{i}", "d1", chunk_index=i) for i in range(3)])
        await store.delete_document("d1", "bank-1")
        # All chunks for the doc must be gone.
        chunks_after = await store.list_chunks("d1", "bank-1")
        assert chunks_after == []


class TestChunkCRUD:
    @pytest.mark.asyncio
    async def test_store_and_list_chunks_in_order(self, store):
        await store.store_document(_doc("d1"))
        await store.store_chunks([
            _chunk("c2", "d1", chunk_index=2),
            _chunk("c0", "d1", chunk_index=0),
            _chunk("c1", "d1", chunk_index=1),
        ])
        listed = await store.list_chunks("d1", "bank-1")
        assert [c.chunk_index for c in listed] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_get_chunk(self, store):
        await store.store_document(_doc("d1"))
        await store.store_chunks([_chunk("c0", "d1")])
        got = await store.get_chunk("c0", "bank-1")
        assert got is not None
        assert got.id == "c0"

    @pytest.mark.asyncio
    async def test_get_chunk_returns_none_for_missing(self, store):
        assert await store.get_chunk("nope", "bank-1") is None


class TestChunkDedup:
    @pytest.mark.asyncio
    async def test_chunk_dedup_returns_existing_id(self, store):
        await store.store_document(_doc("d1"))
        first = await store.store_chunks([_chunk("c-orig", "d1", content_hash="h0")])
        second = await store.store_chunks([
            _chunk("c-DIFFERENT", "d1", chunk_index=99, content_hash="h0"),
        ])
        assert first == ["c-orig"]
        assert second == ["c-orig"]

    @pytest.mark.asyncio
    async def test_find_chunk_by_hash(self, store):
        await store.store_document(_doc("d1"))
        await store.store_chunks([_chunk("c0", "d1", content_hash="hash-Y")])
        found = await store.find_chunk_by_hash("hash-Y", "bank-1")
        assert found is not None
        assert found.id == "c0"

    @pytest.mark.asyncio
    async def test_find_chunk_by_hash_returns_none_for_missing(self, store):
        assert await store.find_chunk_by_hash("no-such", "bank-1") is None

    @pytest.mark.asyncio
    async def test_chunk_hash_dedup_isolates_by_bank(self, store):
        """Same chunk hash in two banks does not dedup across banks."""
        await store.store_document(_doc("d-a", bank_id="bank-1"))
        await store.store_document(_doc("d-b", bank_id="bank-2"))
        a = await store.store_chunks([_chunk("c-a", "d-a", bank_id="bank-1", content_hash="h")])
        b = await store.store_chunks([_chunk("c-b", "d-b", bank_id="bank-2", content_hash="h")])
        assert a == ["c-a"]
        assert b == ["c-b"]


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_is_healthy(self, store):
        h = await store.health()
        assert h.healthy is True
