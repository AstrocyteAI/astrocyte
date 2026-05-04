from __future__ import annotations

import pytest

from astrocyte.pipeline.mental_model import MentalModelService
from astrocyte.testing.in_memory import InMemoryMentalModelStore


@pytest.mark.asyncio
async def test_mental_model_crud_and_refresh() -> None:
    """End-to-end CRUD via the first-class :class:`MentalModelStore` SPI.

    Mirrors the prior wiki-piggyback test but uses the dedicated
    :class:`InMemoryMentalModelStore`. Behavior is identical from the
    service consumer's perspective — that's the WHOLE POINT of breaking
    out the SPI: the gateway / endpoint code doesn't change.
    """
    store = InMemoryMentalModelStore()
    service = MentalModelService(store)

    created = await service.create(
        bank_id="bank-mm",
        model_id="model:alice",
        title="Alice",
        content="Alice prefers concise updates.",
        scope="person:alice",
        source_ids=["m1"],
    )

    assert created.model_id == "model:alice"
    assert created.revision == 1

    listed = await service.list("bank-mm", scope="person:alice")
    assert [model.model_id for model in listed] == ["model:alice"]

    refreshed = await service.refresh(
        bank_id="bank-mm",
        model_id="model:alice",
        content="Alice prefers concise updates and async summaries.",
        source_ids=["m1", "m2"],
    )

    assert refreshed is not None
    assert refreshed.revision == 2
    assert refreshed.source_ids == ["m1", "m2"]
    assert "async summaries" in refreshed.content

    # Extract the side-effect from the assert so that running with `python -O`
    # (which strips assertions) still performs the delete.
    deleted = await service.delete("bank-mm", "model:alice")
    assert deleted is True
    assert await service.get("bank-mm", "model:alice") is None


@pytest.mark.asyncio
async def test_refresh_missing_returns_none() -> None:
    """``refresh()`` requires the model to exist — returns None for typos."""
    service = MentalModelService(InMemoryMentalModelStore())
    result = await service.refresh(
        bank_id="bank-mm",
        model_id="never-created",
        content="x",
    )
    assert result is None


@pytest.mark.asyncio
async def test_refresh_preserves_source_ids_when_omitted() -> None:
    """Refresh without source_ids reuses the prior source list — common
    case where the caller is updating content without re-extracting
    provenance."""
    store = InMemoryMentalModelStore()
    service = MentalModelService(store)
    await service.create(
        bank_id="bank-mm",
        model_id="m1",
        title="T",
        content="v1",
        source_ids=["mem-a", "mem-b"],
    )

    # No source_ids passed → preserves ["mem-a", "mem-b"].
    refreshed = await service.refresh(bank_id="bank-mm", model_id="m1", content="v2")

    assert refreshed is not None
    assert refreshed.source_ids == ["mem-a", "mem-b"]
    assert refreshed.content == "v2"


@pytest.mark.asyncio
async def test_in_memory_store_archives_revision_history() -> None:
    """The in-memory store keeps prior revisions accessible for testing —
    mirrors what PostgresMentalModelStore archives into
    ``astrocyte_mental_model_versions``."""
    store = InMemoryMentalModelStore()
    service = MentalModelService(store)

    await service.create(bank_id="b1", model_id="m", title="T", content="v1")
    await service.refresh(bank_id="b1", model_id="m", content="v2")
    await service.refresh(bank_id="b1", model_id="m", content="v3")

    history = store.revision_history("m", "b1")
    # Two prior revisions archived (v1 and v2); v3 is the current row.
    assert [m.content for m in history] == ["v1", "v2"]
    current = await store.get("m", "b1")
    assert current.content == "v3"
    assert current.revision == 3
