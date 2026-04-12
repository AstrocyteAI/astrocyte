"""Unit tests for KafkaStreamIngestSource (package-local)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from astrocyte.config import SourceConfig

from astrocyte_ingestion_kafka import KafkaStreamIngestSource


@pytest.mark.asyncio
async def test_handle_record_calls_retain() -> None:
    retained: list[object] = []

    async def retain(text: str, bank_id: str, **kwargs: object):
        from astrocyte.types import RetainResult

        retained.append((text, bank_id))
        return RetainResult(stored=True, memory_id="m1")

    cfg = SourceConfig(
        type="stream",
        driver="kafka",
        url="localhost:9092",
        topic="events",
        consumer_group="cg",
        target_bank="bank-a",
    )
    src = KafkaStreamIngestSource("k", cfg, retain=retain)
    msg = SimpleNamespace(value=b'{"content":"x","principal":"user:p"}', topic="events", offset=3)
    await src._handle_record(msg)
    assert len(retained) == 1
    assert retained[0] == ("x", "bank-a")
