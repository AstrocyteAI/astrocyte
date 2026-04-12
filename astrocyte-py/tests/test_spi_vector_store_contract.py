"""Reference SPI contract tests for :class:`~astrocyte.provider.VectorStore`.

M5 adapters should replicate this suite against real backends (see ``docs/_design/m5-adapter-packages.md``).
"""

from __future__ import annotations

import pytest

from astrocyte.provider import VectorStore, check_spi_version
from astrocyte.testing.in_memory import InMemoryVectorStore
from astrocyte.types import VectorFilters, VectorItem


def _vec(n: int = 128) -> list[float]:
    return [1.0 / (i + 1) for i in range(n)]


@pytest.fixture
def store() -> InMemoryVectorStore:
    return InMemoryVectorStore()


@pytest.mark.asyncio
async def test_spi_version_accepted(store: InMemoryVectorStore) -> None:
    assert check_spi_version(store, "VectorStore") == 1


@pytest.mark.asyncio
async def test_isinstance_vector_store_protocol(store: InMemoryVectorStore) -> None:
    assert isinstance(store, VectorStore)


@pytest.mark.asyncio
async def test_store_empty_returns_empty_ids(store: InMemoryVectorStore) -> None:
    assert await store.store_vectors([]) == []


@pytest.mark.asyncio
async def test_health_returns_status(store: InMemoryVectorStore) -> None:
    h = await store.health()
    assert h.healthy is True


@pytest.mark.asyncio
async def test_roundtrip_store_search_delete_list(store: InMemoryVectorStore) -> None:
    bid = "bank-a"
    q = _vec()
    item = VectorItem(id="v1", bank_id=bid, vector=q, text="hello", tags=["t1"])
    ids = await store.store_vectors([item])
    assert ids == ["v1"]

    hits = await store.search_similar(q, bid, limit=5)
    assert len(hits) == 1
    assert hits[0].id == "v1"

    n = await store.delete(["v1"], bid)
    assert n == 1
    hits2 = await store.search_similar(q, bid, limit=5)
    assert hits2 == []

    listed = await store.list_vectors(bid, offset=0, limit=10)
    assert listed == []


@pytest.mark.asyncio
async def test_bank_isolation_on_delete(store: InMemoryVectorStore) -> None:
    q = _vec()
    await store.store_vectors(
        [
            VectorItem(id="a", bank_id="b1", vector=q, text="x"),
            VectorItem(id="b", bank_id="b2", vector=q, text="y"),
        ]
    )
    deleted_wrong_bank = await store.delete(["a"], "b2")
    assert deleted_wrong_bank == 0
    h1 = await store.search_similar(q, "b1", limit=5)
    assert len(h1) == 1
    h2 = await store.search_similar(q, "b2", limit=5)
    assert len(h2) == 1


@pytest.mark.asyncio
async def test_search_respects_tag_filter(store: InMemoryVectorStore) -> None:
    q = _vec()
    await store.store_vectors(
        [
            VectorItem(id="x1", bank_id="b", vector=q, text="a", tags=["keep"]),
            VectorItem(id="x2", bank_id="b", vector=q, text="b", tags=["drop"]),
        ]
    )
    fl = VectorFilters(tags=["keep"])
    hits = await store.search_similar(q, "b", limit=10, filters=fl)
    assert len(hits) == 1
    assert hits[0].id == "x1"


@pytest.mark.asyncio
async def test_roundtrip_single_element_vector(store: InMemoryVectorStore) -> None:
    bid = "bank-single"
    q = _vec(1)
    item = VectorItem(id="s1", bank_id=bid, vector=q, text="single")
    ids = await store.store_vectors([item])
    assert ids == ["s1"]

    hits = await store.search_similar(q, bid, limit=5)
    assert len(hits) == 1
    assert hits[0].id == "s1"


@pytest.mark.asyncio
async def test_empty_vector_roundtrip(store: InMemoryVectorStore) -> None:
    """Empty query/stored vectors yield zero cosine similarity but remain searchable."""
    bid = "bank-empty"
    q: list[float] = []
    item = VectorItem(id="e1", bank_id=bid, vector=q, text="empty")
    ids = await store.store_vectors([item])
    assert ids == ["e1"]

    hits = await store.search_similar(q, bid, limit=5)
    assert len(hits) == 1
    assert hits[0].id == "e1"


@pytest.mark.asyncio
async def test_roundtrip_large_vector(store: InMemoryVectorStore) -> None:
    bid = "bank-large"
    q = _vec(4096)
    item = VectorItem(id="l1", bank_id=bid, vector=q, text="large")
    ids = await store.store_vectors([item])
    assert ids == ["l1"]

    hits = await store.search_similar(q, bid, limit=5)
    assert len(hits) == 1
    assert hits[0].id == "l1"
