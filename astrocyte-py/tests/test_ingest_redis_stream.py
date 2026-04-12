"""Redis stream ingest + registry wiring."""

from __future__ import annotations

import pytest

from astrocyte._discovery import resolve_provider
from astrocyte.config import SourceConfig
from astrocyte.errors import ConfigError
from astrocyte.ingest.payload import parse_ingest_stream_fields
from astrocyte.ingest.registry import SourceRegistry
from astrocyte.ingest.runtime import retain_callable_for_astrocyte


def test_parse_ingest_stream_fields_payload_json() -> None:
    fields = {"payload": '{"content":"hello","principal":"user:1","content_type":"text"}'}
    text, pr, ct, meta = parse_ingest_stream_fields(fields)
    assert text == "hello"
    assert pr == "user:1"
    assert ct == "text"
    assert meta is None


def test_parse_ingest_stream_fields_flat() -> None:
    fields = {"content": "x", "principal": "user:2", "content_type": "document"}
    text, pr, ct, _ = parse_ingest_stream_fields(fields)
    assert text == "x"
    assert pr == "user:2"
    assert ct == "document"


def test_registry_stream_requires_retain() -> None:
    sources = {
        "ev": SourceConfig(
            type="stream",
            url="redis://localhost:6379/0",
            topic="events",
            consumer_group="cg",
            target_bank="bank-a",
        ),
    }
    with pytest.raises(ConfigError, match="requires retain"):
        SourceRegistry.from_sources_config(sources, retain=None)


def test_registry_stream_with_retain_registers() -> None:
    try:
        resolve_provider("redis", "ingest_stream_drivers")
    except LookupError:
        pytest.skip("astrocyte-ingestion-redis not installed (no redis stream driver entry point)")

    async def fake_retain(*_a: object, **_k: object):
        from astrocyte.types import RetainResult

        return RetainResult(stored=True, memory_id="m1")

    sources = {
        "ev": SourceConfig(
            type="stream",
            url="redis://localhost:6379/0",
            topic="events",
            consumer_group="cg",
            target_bank="bank-a",
        ),
    }
    reg = SourceRegistry.from_sources_config(sources, retain=fake_retain)
    assert len(reg.all_sources()) == 1
    ev = reg.get("ev")
    assert ev is not None
    assert ev.source_type == "stream"


@pytest.mark.asyncio
async def test_retain_callable_for_astrocyte_passes_context() -> None:
    calls: list[object] = []

    class _FakeBrain:
        async def retain(self, text: str, bank_id: str, **kwargs: object):
            calls.append((text, bank_id, kwargs.get("context")))
            from astrocyte.types import RetainResult

            return RetainResult(stored=True, memory_id="x")

    brain = _FakeBrain()
    fn = retain_callable_for_astrocyte(brain)  # type: ignore[arg-type]
    await fn("t", "b", principal="user:z")
    assert len(calls) == 1
    ctx = calls[0][2]
    assert ctx is not None
    assert getattr(ctx, "principal", None) == "user:z"
