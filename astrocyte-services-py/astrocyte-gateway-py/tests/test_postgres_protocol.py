"""Tests for PostgresStore protocol parity (list_vectors, memory_layer).

These tests verify protocol compliance using InMemoryVectorStore as the
reference implementation since PostgreSQL is not available in CI.
The PostgresStore SQL is tested structurally (schema, column presence).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from astrocyte.testing.in_memory import InMemoryVectorStore
from astrocyte.types import VectorItem

_pgvector_available = True
try:
    from astrocyte_postgres.store import PostgresStore
except ImportError:
    _pgvector_available = False


def _make_item(vid: str, bank_id: str, *, memory_layer: str | None = None) -> VectorItem:
    return VectorItem(
        id=vid,
        bank_id=bank_id,
        vector=[1.0, 0.0, 0.0],
        text=f"test-{vid}",
        memory_layer=memory_layer,
    )


# ---------------------------------------------------------------------------
# list_vectors pagination (InMemoryVectorStore as reference)
# ---------------------------------------------------------------------------


class TestListVectorsPagination:
    def test_basic_pagination(self):
        async def _run():
            vs = InMemoryVectorStore()
            items = [_make_item(f"v{i:03d}", "bank-1") for i in range(10)]
            await vs.store_vectors(items)

            page1 = await vs.list_vectors("bank-1", offset=0, limit=4)
            page2 = await vs.list_vectors("bank-1", offset=4, limit=4)
            page3 = await vs.list_vectors("bank-1", offset=8, limit=4)

            assert len(page1) == 4
            assert len(page2) == 4
            assert len(page3) == 2

            all_ids = {v.id for p in [page1, page2, page3] for v in p}
            assert len(all_ids) == 10

        asyncio.run(_run())

    def test_empty_bank_returns_empty(self):
        async def _run():
            vs = InMemoryVectorStore()
            result = await vs.list_vectors("nonexistent")
            assert result == []

        asyncio.run(_run())

    def test_offset_beyond_end(self):
        async def _run():
            vs = InMemoryVectorStore()
            await vs.store_vectors([_make_item("v1", "bank-1")])
            result = await vs.list_vectors("bank-1", offset=100)
            assert result == []

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# memory_layer roundtrip (InMemoryVectorStore as reference)
# ---------------------------------------------------------------------------


class TestMemoryLayerRoundtrip:
    def test_store_and_list_preserves_memory_layer(self):
        async def _run():
            vs = InMemoryVectorStore()
            await vs.store_vectors([
                _make_item("v1", "bank-1", memory_layer="fact"),
                _make_item("v2", "bank-1", memory_layer="observation"),
                _make_item("v3", "bank-1", memory_layer=None),
            ])

            items = await vs.list_vectors("bank-1")
            layer_map = {v.id: v.memory_layer for v in items}
            assert layer_map["v1"] == "fact"
            assert layer_map["v2"] == "observation"
            assert layer_map["v3"] is None

        asyncio.run(_run())

    def test_search_returns_memory_layer(self):
        async def _run():
            vs = InMemoryVectorStore()
            await vs.store_vectors([
                _make_item("v1", "bank-1", memory_layer="model"),
            ])

            hits = await vs.search_similar([1.0, 0.0, 0.0], "bank-1", limit=1)
            assert len(hits) == 1
            assert hits[0].memory_layer == "model"

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# PostgresStore structural checks (no PostgreSQL needed)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pgvector_available, reason="pgvector deps not installed")
class TestPostgresStoreStructure:
    def test_has_list_vectors_method(self):
        assert hasattr(PostgresStore, "list_vectors")
        sig = inspect.signature(PostgresStore.list_vectors)
        params = list(sig.parameters.keys())
        assert "bank_id" in params
        assert "offset" in params
        assert "limit" in params

    def test_schema_includes_memory_layer(self):
        source = inspect.getsource(PostgresStore._ensure_schema)
        assert "memory_layer" in source

    def test_store_vectors_includes_memory_layer(self):
        source = inspect.getsource(PostgresStore.store_vectors)
        assert "memory_layer" in source

    def test_search_similar_includes_memory_layer(self):
        source = inspect.getsource(PostgresStore.search_similar)
        assert "memory_layer" in source

    def test_sql_migration_uses_configurable_vector_width(self):
        repo_root = Path(__file__).resolve().parents[3]
        migration = repo_root / "adapters-storage-py/astrocyte-postgres/migrations/002_astrocytes_vectors.sql"
        migrate_script = repo_root / "adapters-storage-py/astrocyte-postgres/scripts/migrate.sh"

        assert "embedding vector(:embedding_dimensions) NOT NULL" in migration.read_text()
        assert "ASTROCYTE_EMBEDDING_DIMENSIONS" in migrate_script.read_text()

    def test_spi_version_is_1(self):
        assert PostgresStore.SPI_VERSION == 1
