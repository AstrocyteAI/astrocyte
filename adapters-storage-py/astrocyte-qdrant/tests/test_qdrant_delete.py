from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrocyte_qdrant import QdrantVectorStore


@pytest.mark.asyncio
async def test_delete_uses_retrieve_and_returns_deleted_count() -> None:
    store = QdrantVectorStore(url="http://example.com", collection_name="test", vector_size=4)
    store._ensure_collection = AsyncMock()  # noqa: SLF001
    store._client = SimpleNamespace(  # noqa: SLF001
        retrieve=AsyncMock(return_value=[object()]),
        delete=AsyncMock(),
    )

    deleted = await store.delete(["m1"], bank_id="b1")

    assert deleted == 1
    store._client.retrieve.assert_awaited_once()
    store._client.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_skips_delete_when_points_are_missing() -> None:
    store = QdrantVectorStore(url="http://example.com", collection_name="test", vector_size=4)
    store._ensure_collection = AsyncMock()  # noqa: SLF001
    store._client = SimpleNamespace(  # noqa: SLF001
        retrieve=AsyncMock(return_value=[]),
        delete=AsyncMock(),
    )

    deleted = await store.delete(["missing"], bank_id="b1")

    assert deleted == 0
    store._client.retrieve.assert_awaited_once()
    store._client.delete.assert_not_awaited()
