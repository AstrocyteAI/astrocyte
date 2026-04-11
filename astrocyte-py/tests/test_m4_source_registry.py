"""M4 — IngestSource protocol and SourceRegistry (TDD)."""

from __future__ import annotations

import pytest

from astrocyte.config import SourceConfig
from astrocyte.ingest.registry import SourceRegistry
from astrocyte.ingest.source import WebhookIngestSource


class TestWebhookIngestSource:
    def test_implements_protocol_fields(self):
        cfg = SourceConfig(type="webhook", target_bank="b1")
        src = WebhookIngestSource("my-hook", cfg)
        assert src.source_id == "my-hook"
        assert src.source_type == "webhook"

    @pytest.mark.asyncio
    async def test_start_stop_health(self):
        cfg = SourceConfig(type="webhook", target_bank="b1")
        src = WebhookIngestSource("hook", cfg)
        h0 = await src.health_check()
        assert h0.healthy is False

        await src.start()
        h1 = await src.health_check()
        assert h1.healthy is True

        await src.stop()
        h2 = await src.health_check()
        assert h2.healthy is False


class TestSourceRegistry:
    def test_register_get(self):
        cfg = SourceConfig(type="webhook", target_bank="x")
        src = WebhookIngestSource("a", cfg)
        reg = SourceRegistry()
        reg.register(src)
        assert reg.get("a") is src

    @pytest.mark.asyncio
    async def test_start_all_starts_sources(self):
        cfg = SourceConfig(type="webhook", target_bank="x")
        src = WebhookIngestSource("w", cfg)
        reg = SourceRegistry()
        reg.register(src)
        await reg.start_all()
        st = await src.health_check()
        assert st.healthy is True
        await reg.stop_all()

    def test_from_config_builds_webhook_sources(self):
        sources = {
            "tavus": SourceConfig(type="webhook", target_bank="ingest-1"),
        }
        reg = SourceRegistry.from_sources_config(sources)
        assert reg.get("tavus") is not None
        assert reg.get("tavus").source_type == "webhook"
