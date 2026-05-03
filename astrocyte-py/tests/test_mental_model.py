from __future__ import annotations

import pytest

from astrocyte.pipeline.mental_model import MentalModelService
from astrocyte.testing.in_memory import InMemoryWikiStore


@pytest.mark.asyncio
async def test_mental_model_crud_and_refresh() -> None:
    store = InMemoryWikiStore()
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

    assert await service.delete("bank-mm", "model:alice") is True
    assert await service.get("bank-mm", "model:alice") is None
