"""M4 — external data ingest (webhooks; Redis / Kafka stream consumers; polls later).

Use :func:`astrocyte.ingest.webhook.handle_webhook_ingest` from an HTTP layer with the raw
request body (needed for HMAC). Optional ASGI helper: ``astrocyte.ingest.fastapi_app.create_ingest_webhook_app`` (install ``astrocyte[gateway]``; Starlette app, uvicorn-compatible).
Stream sources (``type: stream``, ``driver: kafka`` / ``redis``) need ``astrocyte[stream]`` (``astrocyte-ingestion-kafka``, ``astrocyte-ingestion-redis``) and ``retain=`` when building :class:`SourceRegistry`.
See ``docs/_design/product-roadmap-v1.md`` (M4).
"""

from __future__ import annotations

from typing import Any

from astrocyte.ingest.bank_resolve import resolve_ingest_bank_id
from astrocyte.ingest.hmac_auth import compute_hmac_sha256_hex, verify_hmac_sha256
from astrocyte.ingest.payload import parse_ingest_kafka_value, parse_ingest_stream_fields
from astrocyte.ingest.registry import SourceRegistry
from astrocyte.ingest.runtime import retain_callable_for_astrocyte
from astrocyte.ingest.source import IngestSource, WebhookIngestSource
from astrocyte.ingest.supervisor import IngestSupervisor, install_shutdown_signals, merge_source_health
from astrocyte.ingest.webhook import WebhookIngestResult, handle_webhook_ingest


def __getattr__(name: str) -> Any:
    if name == "RedisStreamIngestSource":
        try:
            from astrocyte_ingestion_redis import RedisStreamIngestSource as _RedisStreamIngestSource
        except ImportError as e:
            raise AttributeError(
                "RedisStreamIngestSource requires astrocyte-ingestion-redis. "
                "Install: pip install astrocyte-ingestion-redis or pip install 'astrocyte[stream]'."
            ) from e
        return _RedisStreamIngestSource
    if name == "KafkaStreamIngestSource":
        try:
            from astrocyte_ingestion_kafka import KafkaStreamIngestSource as _KafkaStreamIngestSource
        except ImportError as e:
            raise AttributeError(
                "KafkaStreamIngestSource requires astrocyte-ingestion-kafka. "
                "Install: pip install astrocyte-ingestion-kafka or pip install 'astrocyte[stream]'."
            ) from e
        return _KafkaStreamIngestSource
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "IngestSource",
    "WebhookIngestSource",
    "RedisStreamIngestSource",
    "KafkaStreamIngestSource",
    "SourceRegistry",
    "handle_webhook_ingest",
    "WebhookIngestResult",
    "retain_callable_for_astrocyte",
    "IngestSupervisor",
    "merge_source_health",
    "install_shutdown_signals",
    "parse_ingest_stream_fields",
    "parse_ingest_kafka_value",
    "resolve_ingest_bank_id",
    "compute_hmac_sha256_hex",
    "verify_hmac_sha256",
]
