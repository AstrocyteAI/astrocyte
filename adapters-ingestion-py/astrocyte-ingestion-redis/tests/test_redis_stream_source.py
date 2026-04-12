"""Unit tests for RedisStreamIngestSource (package-local)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from astrocyte.config import SourceConfig

from astrocyte_ingestion_redis import RedisStreamIngestSource


@pytest.mark.asyncio
async def test_handle_one_calls_retain_and_xack() -> None:
    retained: list[object] = []

    async def retain(text: str, bank_id: str, **kwargs: object):
        from astrocyte.types import RetainResult

        retained.append((text, bank_id, kwargs))
        return RetainResult(stored=True, memory_id="m1")

    cfg = SourceConfig(
        type="stream",
        url="redis://localhost:6379/0",
        topic="events",
        consumer_group="cg",
        target_bank="bank-a",
    )
    src = RedisStreamIngestSource("ev", cfg, retain=retain)
    r = AsyncMock()
    await src._handle_one(r, "events", "cg", "1-0", {"content": "c1", "principal": "user:u"})
    r.xack.assert_awaited_once_with("events", "cg", "1-0")
    assert len(retained) == 1
    assert retained[0][0] == "c1"
    assert retained[0][1] == "bank-a"
