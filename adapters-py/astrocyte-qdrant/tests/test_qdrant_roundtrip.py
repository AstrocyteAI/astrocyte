"""Integration tests against Qdrant (CI service container or local)."""

from __future__ import annotations

import os
import uuid

import pytest
from astrocyte.types import VectorItem

from astrocyte_qdrant import QdrantVectorStore

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

QDRANT_URL = os.environ.get("ASTROCYTE_QDRANT_URL", "http://127.0.0.1:6333")


async def _cleanup(store: QdrantVectorStore, name: str) -> None:
    try:
        await store._client.delete_collection(collection_name=name)  # noqa: SLF001
    except Exception:
        pass


@pytest.mark.asyncio
async def test_health(require_qdrant: None) -> None:
    name = f"ast_test_{uuid.uuid4().hex[:12]}"
    store = QdrantVectorStore(url=QDRANT_URL, collection_name=name, vector_size=4)
    try:
        h = await store.health()
        assert h.healthy is True
    finally:
        await _cleanup(store, name)


@pytest.mark.asyncio
async def test_store_search_delete_roundtrip(require_qdrant: None) -> None:
    name = f"ast_test_{uuid.uuid4().hex[:12]}"
    store = QdrantVectorStore(url=QDRANT_URL, collection_name=name, vector_size=4)
    try:
        bank = "b1"
        v = [0.0, 0.0, 1.0, 0.0]
        items = [
            VectorItem(id="m1", bank_id=bank, vector=v, text="hello qdrant"),
        ]
        ids = await store.store_vectors(items)
        assert ids == ["m1"]

        hits = await store.search_similar(v, bank_id=bank, limit=5)
        assert len(hits) >= 1
        assert hits[0].text == "hello qdrant"
        assert 0.0 <= hits[0].score <= 1.0

        n = await store.delete(["m1"], bank_id=bank)
        assert n == 1
        hits2 = await store.search_similar(v, bank_id=bank, limit=5)
        assert hits2 == []
    finally:
        await _cleanup(store, name)
