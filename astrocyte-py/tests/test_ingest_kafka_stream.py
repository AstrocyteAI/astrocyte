"""Kafka stream ingest (payload + registry; driver implementation in astrocyte-ingestion-kafka)."""

from __future__ import annotations

import pytest

from astrocyte._discovery import resolve_provider
from astrocyte.config import SourceConfig
from astrocyte.errors import ConfigError
from astrocyte.ingest.payload import parse_ingest_kafka_value
from astrocyte.ingest.registry import SourceRegistry


def test_parse_ingest_kafka_value_json() -> None:
    raw = b'{"content":"hi","principal":"user:1"}'
    text, pr, ct, meta = parse_ingest_kafka_value(raw)
    assert text == "hi"
    assert pr == "user:1"
    assert ct == "text"
    assert meta is None


def test_parse_ingest_kafka_value_plain_text() -> None:
    text, pr, ct, meta = parse_ingest_kafka_value(b"plain line")
    assert text == "plain line"
    assert pr is None
    assert ct == "text"


def test_registry_unknown_stream_driver() -> None:
    async def fake_retain(*_a: object, **_k: object):
        from astrocyte.types import RetainResult

        return RetainResult(stored=True, memory_id="m1")

    sources = {
        "x": SourceConfig(
            type="stream",
            driver="not-a-real-driver",
            url="localhost:9092",
            topic="events",
            consumer_group="cg",
            target_bank="bank-a",
        ),
    }
    with pytest.raises(ConfigError, match="not installed or unknown"):
        SourceRegistry.from_sources_config(sources, retain=fake_retain)


def test_registry_kafka_registers() -> None:
    try:
        resolve_provider("kafka", "ingest_stream_drivers")
    except LookupError:
        pytest.skip("astrocyte-ingestion-kafka not installed (no kafka stream driver entry point)")

    async def fake_retain(*_a: object, **_k: object):
        from astrocyte.types import RetainResult

        return RetainResult(stored=True, memory_id="m1")

    sources = {
        "k": SourceConfig(
            type="stream",
            driver="kafka",
            url="localhost:9092",
            topic="events",
            consumer_group="cg",
            target_bank="bank-a",
        ),
    }
    reg = SourceRegistry.from_sources_config(sources, retain=fake_retain)
    k = reg.get("k")
    assert k is not None
    assert k.source_type == "stream"
