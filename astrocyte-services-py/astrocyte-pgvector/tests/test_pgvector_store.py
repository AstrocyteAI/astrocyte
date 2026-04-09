"""Integration tests for PgVectorStore against a real PostgreSQL + pgvector database."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from astrocyte.types import VectorFilters
from astrocyte_pgvector.store import PgVectorStore

from .conftest import DIM, make_item


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStoreVectors:
    async def test_store_single_item(self, store: PgVectorStore):
        item = make_item("v1", text="hello world", vector=[1.0, 0.0, 0.0])
        ids = await store.store_vectors([item])
        assert ids == ["v1"]

        items = await store.list_vectors("bank-1")
        assert len(items) == 1
        assert items[0].id == "v1"
        assert items[0].text == "hello world"
        assert items[0].bank_id == "bank-1"

    async def test_store_multiple_items(self, store: PgVectorStore):
        items = [make_item(f"v{i}") for i in range(3)]
        ids = await store.store_vectors(items)
        assert len(ids) == 3

        result = await store.list_vectors("bank-1")
        assert len(result) == 3

    async def test_upsert_overwrites(self, store: PgVectorStore):
        item = make_item("v1", text="original", vector=[1.0, 0.0, 0.0])
        await store.store_vectors([item])

        updated = make_item("v1", text="updated", vector=[0.0, 1.0, 0.0])
        await store.store_vectors([updated])

        items = await store.list_vectors("bank-1")
        assert len(items) == 1
        assert items[0].text == "updated"

    async def test_dimension_mismatch_raises(self, store: PgVectorStore):
        item = make_item("v1", vector=[1.0, 0.0])  # 2-dim, expects 3
        with pytest.raises(ValueError, match="embedding_dimensions"):
            await store.store_vectors([item])


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearchSimilar:
    async def test_basic_search(self, store: PgVectorStore):
        await store.store_vectors([
            make_item("a", vector=[1.0, 0.0, 0.0]),
            make_item("b", vector=[0.0, 1.0, 0.0]),
            make_item("c", vector=[0.0, 0.0, 1.0]),
        ])
        hits = await store.search_similar([1.0, 0.0, 0.0], "bank-1", limit=3)
        assert len(hits) == 3
        assert hits[0].id == "a"

    async def test_search_filters_by_bank(self, store: PgVectorStore):
        await store.store_vectors([
            make_item("a", bank_id="bank-1", vector=[1.0, 0.0, 0.0]),
            make_item("b", bank_id="bank-2", vector=[1.0, 0.0, 0.0]),
        ])
        hits = await store.search_similar([1.0, 0.0, 0.0], "bank-1")
        assert len(hits) == 1
        assert hits[0].id == "a"

    async def test_search_with_tag_filter(self, store: PgVectorStore):
        await store.store_vectors([
            make_item("a", vector=[1.0, 0.0, 0.0], tags=["important"]),
            make_item("b", vector=[0.9, 0.1, 0.0], tags=["trivial"]),
        ])
        hits = await store.search_similar(
            [1.0, 0.0, 0.0],
            "bank-1",
            filters=VectorFilters(tags=["important"]),
        )
        assert len(hits) == 1
        assert hits[0].id == "a"

    async def test_search_with_fact_type_filter(self, store: PgVectorStore):
        await store.store_vectors([
            make_item("a", vector=[1.0, 0.0, 0.0], fact_type="world"),
            make_item("b", vector=[0.9, 0.1, 0.0], fact_type="experience"),
        ])
        hits = await store.search_similar(
            [1.0, 0.0, 0.0],
            "bank-1",
            filters=VectorFilters(fact_types=["world"]),
        )
        assert len(hits) == 1
        assert hits[0].id == "a"

    async def test_search_score_range(self, store: PgVectorStore):
        await store.store_vectors([
            make_item("a", vector=[1.0, 0.0, 0.0]),
            make_item("b", vector=[0.0, 1.0, 0.0]),
        ])
        hits = await store.search_similar([1.0, 0.0, 0.0], "bank-1")
        for hit in hits:
            assert 0.0 <= hit.score <= 1.0

    async def test_search_dimension_mismatch_raises(self, store: PgVectorStore):
        with pytest.raises(ValueError, match="embedding_dimensions"):
            await store.search_similar([1.0, 0.0], "bank-1")


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_existing(self, store: PgVectorStore):
        await store.store_vectors([make_item(f"v{i}") for i in range(3)])
        deleted = await store.delete(["v0"], "bank-1")
        assert deleted == 1

        remaining = await store.list_vectors("bank-1")
        assert len(remaining) == 2
        assert {r.id for r in remaining} == {"v1", "v2"}

    async def test_delete_respects_bank_isolation(self, store: PgVectorStore):
        await store.store_vectors([
            make_item("v1", bank_id="bank-1"),
            make_item("v2", bank_id="bank-2"),
        ])
        deleted = await store.delete(["v1", "v2"], "bank-1")
        assert deleted == 1  # only v1 is in bank-1

        # bank-2 item untouched
        remaining = await store.list_vectors("bank-2")
        assert len(remaining) == 1
        assert remaining[0].id == "v2"

    async def test_delete_nonexistent_returns_zero(self, store: PgVectorStore):
        deleted = await store.delete(["nope"], "bank-1")
        assert deleted == 0

    async def test_delete_empty_ids(self, store: PgVectorStore):
        deleted = await store.delete([], "bank-1")
        assert deleted == 0


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestListVectors:
    async def test_pagination(self, store: PgVectorStore):
        items = [make_item(f"v{i:02d}") for i in range(10)]
        await store.store_vectors(items)

        all_ids: set[str] = set()
        offset = 0
        while True:
            page = await store.list_vectors("bank-1", offset=offset, limit=4)
            if not page:
                break
            all_ids.update(v.id for v in page)
            offset += len(page)

        assert all_ids == {f"v{i:02d}" for i in range(10)}

    async def test_ordered_by_id(self, store: PgVectorStore):
        await store.store_vectors([make_item("c"), make_item("a"), make_item("b")])
        items = await store.list_vectors("bank-1")
        assert [i.id for i in items] == ["a", "b", "c"]

    async def test_offset_beyond_end(self, store: PgVectorStore):
        await store.store_vectors([make_item("v1")])
        result = await store.list_vectors("bank-1", offset=100)
        assert result == []

    async def test_empty_bank(self, store: PgVectorStore):
        result = await store.list_vectors("nonexistent")
        assert result == []


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_healthy(self, store: PgVectorStore):
        status = await store.health()
        assert status.healthy is True
        assert "connected" in status.message


# ---------------------------------------------------------------------------
# Memory layer roundtrip
# ---------------------------------------------------------------------------


class TestMemoryLayer:
    async def test_roundtrip_via_list(self, store: PgVectorStore):
        await store.store_vectors([
            make_item("v1", vector=[1.0, 0.0, 0.0], memory_layer="fact"),
            make_item("v2", vector=[0.0, 1.0, 0.0], memory_layer="observation"),
            make_item("v3", vector=[0.0, 0.0, 1.0], memory_layer=None),
        ])
        items = await store.list_vectors("bank-1")
        by_id = {i.id: i for i in items}
        assert by_id["v1"].memory_layer == "fact"
        assert by_id["v2"].memory_layer == "observation"
        assert by_id["v3"].memory_layer is None

    async def test_roundtrip_via_search(self, store: PgVectorStore):
        await store.store_vectors([
            make_item("v1", vector=[1.0, 0.0, 0.0], memory_layer="model"),
        ])
        hits = await store.search_similar([1.0, 0.0, 0.0], "bank-1")
        assert len(hits) == 1
        assert hits[0].memory_layer == "model"


# ---------------------------------------------------------------------------
# Metadata, tags, timestamps
# ---------------------------------------------------------------------------


class TestMetadataAndTags:
    async def test_metadata_roundtrip(self, store: PgVectorStore):
        md = {"key": "value", "count": 42, "nested": {"a": True}}
        await store.store_vectors([make_item("v1", vector=[1.0, 0.0, 0.0], metadata=md)])

        items = await store.list_vectors("bank-1")
        assert items[0].metadata == md

    async def test_null_metadata(self, store: PgVectorStore):
        await store.store_vectors([make_item("v1", vector=[1.0, 0.0, 0.0], metadata=None)])
        items = await store.list_vectors("bank-1")
        assert items[0].metadata is None

    async def test_tags_roundtrip(self, store: PgVectorStore):
        await store.store_vectors([
            make_item("v1", vector=[1.0, 0.0, 0.0], tags=["a", "b", "c"]),
        ])
        items = await store.list_vectors("bank-1")
        assert sorted(items[0].tags) == ["a", "b", "c"]

    async def test_occurred_at_roundtrip(self, store: PgVectorStore):
        ts = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        await store.store_vectors([make_item("v1", vector=[1.0, 0.0, 0.0], occurred_at=ts)])

        items = await store.list_vectors("bank-1")
        assert items[0].occurred_at == ts


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_table_name_raises(self, dsn: str):
        with pytest.raises(ValueError, match="Invalid table name"):
            PgVectorStore(dsn=dsn, table_name="drop; --")

    def test_invalid_dimensions_raises(self, dsn: str):
        with pytest.raises(ValueError, match="embedding_dimensions must be >= 1"):
            PgVectorStore(dsn=dsn, embedding_dimensions=0)
