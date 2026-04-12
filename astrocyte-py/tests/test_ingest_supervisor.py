"""IngestSupervisor — shared lifecycle for stream/webhook sources."""

from __future__ import annotations

import pytest

from astrocyte.config import SourceConfig
from astrocyte.ingest.registry import SourceRegistry
from astrocyte.ingest.source import WebhookIngestSource
from astrocyte.ingest.supervisor import IngestSupervisor, merge_source_health


@pytest.mark.asyncio
async def test_supervisor_start_stop_idempotent() -> None:
    cfg = SourceConfig(type="webhook", target_bank="b")
    reg = SourceRegistry()
    reg.register(WebhookIngestSource("w", cfg))
    sup = IngestSupervisor(reg, stop_timeout_s=None)

    await sup.start()
    await sup.start()  # idempotent
    await sup.stop()
    await sup.stop()  # idempotent


@pytest.mark.asyncio
async def test_health_snapshot_and_merge() -> None:
    cfg = SourceConfig(type="webhook", target_bank="b")
    reg = SourceRegistry()
    reg.register(WebhookIngestSource("w", cfg))
    await reg.start_all()
    sup = IngestSupervisor(reg, stop_timeout_s=None)
    rows = await sup.health_snapshot()
    assert len(rows) == 1
    assert rows[0]["id"] == "w"
    merged = merge_source_health(rows)
    assert merged.healthy is True
    await reg.stop_all()


@pytest.mark.asyncio
async def test_merge_source_health_detects_unhealthy() -> None:
    rows = [{"id": "a", "healthy": False, "message": "x"}]
    m = merge_source_health(rows)
    assert m.healthy is False
    assert "a" in (m.message or "")
