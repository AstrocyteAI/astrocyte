"""Lifecycle and behaviour tests for S3PollIngestSource and S3WebhookIngestSource.

S3 network calls are avoided entirely: we either patch _make_client or drive
handle_webhook() / _ingest_object() with mocked dependencies.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from astrocyte.config import SourceConfig
from astrocyte.errors import IngestError
from astrocyte.types import RetainResult

from astrocyte_ingestion_s3 import S3PollIngestSource, S3WebhookIngestSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(**overrides: Any) -> SourceConfig:
    defaults: dict[str, Any] = {
        "type": "poll",
        "driver": "s3",
        "path": "my-bucket/prefix/",
        "interval_seconds": 60,
        "target_bank": "bank-a",
        "auth": {"access_key": "AKID", "secret_key": "secret"},
    }
    defaults.update(overrides)
    return SourceConfig(**defaults)


def _webhook_cfg(**overrides: Any) -> SourceConfig:
    defaults: dict[str, Any] = {
        "type": "webhook",
        "driver": "s3",
        "url": "https://garage.example.com",
        "path": "my-bucket",
        "target_bank": "bank-a",
        "auth": {"access_key": "AKID", "secret_key": "secret"},
    }
    defaults.update(overrides)
    return SourceConfig(**defaults)


async def _noop_retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
    return RetainResult(stored=True, memory_id="m1")


# ---------------------------------------------------------------------------
# S3PollIngestSource — lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_start_stop() -> None:
    src = S3PollIngestSource("s3-poll", _make_cfg(), retain=_noop_retain)
    await src.start()
    assert src._running is True
    assert src._task is not None

    health = await src.health_check()
    assert health.healthy is True

    await src.stop()
    assert src._running is False
    assert src._task is None


@pytest.mark.asyncio
async def test_poll_start_idempotent() -> None:
    src = S3PollIngestSource("s3-poll", _make_cfg(), retain=_noop_retain)
    await src.start()
    task_first = src._task
    await src.start()  # second call must be a no-op
    assert src._task is task_first
    await src.stop()


@pytest.mark.asyncio
async def test_poll_health_stopped() -> None:
    src = S3PollIngestSource("s3-poll", _make_cfg(), retain=_noop_retain)
    health = await src.health_check()
    assert health.healthy is False


@pytest.mark.asyncio
async def test_poll_bad_config_missing_auth() -> None:
    cfg = SourceConfig(
        type="poll",
        driver="s3",
        path="bucket",
        interval_seconds=60,
        target_bank="bank-a",
        auth={},
    )
    src = S3PollIngestSource("s3-poll", cfg, retain=_noop_retain)
    with pytest.raises(IngestError, match="access_key"):
        await src.start()


# ---------------------------------------------------------------------------
# S3PollIngestSource — _poll_once with mocked S3 client
# ---------------------------------------------------------------------------


def _make_mock_s3_client(objects: list[dict[str, Any]], body: bytes, content_type: str = "text/plain"):
    """Return an async context manager mock that yields a fake S3 client."""

    async def fake_paginate(**kwargs: Any):
        yield {"Contents": objects}

    paginator = MagicMock()
    paginator.paginate = fake_paginate

    async def fake_body_iter():
        yield body

    body_cm = MagicMock()
    body_cm.__aenter__ = AsyncMock(return_value=MagicMock(__aiter__=lambda self: fake_body_iter().__aiter__()))
    body_cm.__aexit__ = AsyncMock(return_value=None)

    s3_client = MagicMock()
    s3_client.get_paginator = MagicMock(return_value=paginator)
    s3_client.get_object = AsyncMock(return_value={"Body": body_cm, "ContentType": content_type})

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=s3_client)
    client_cm.__aexit__ = AsyncMock(return_value=None)

    return client_cm


@pytest.mark.asyncio
async def test_poll_once_retains_new_object() -> None:
    retained: list[tuple[str, str]] = []

    async def retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
        retained.append((text, bank_id))
        return RetainResult(stored=True, memory_id="m1")

    cfg = _make_cfg()
    src = S3PollIngestSource("s3-poll", cfg, retain=retain)

    objects = [{"Key": "prefix/hello.txt", "ETag": '"abc123"'}]
    body = b"Hello from S3!"
    mock_client = _make_mock_s3_client(objects, body)

    with patch("astrocyte_ingestion_s3.source._make_client", return_value=mock_client):
        await src._poll_once()

    assert len(retained) == 1
    assert retained[0][1] == "bank-a"
    assert "Hello from S3!" in retained[0][0]
    # ETag recorded — second poll should skip
    assert src._seen.get("prefix/hello.txt") == "abc123"


@pytest.mark.asyncio
async def test_poll_once_skips_unchanged_etag() -> None:
    retained: list[str] = []

    async def retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
        retained.append(text)
        return RetainResult(stored=True, memory_id="m1")

    cfg = _make_cfg()
    src = S3PollIngestSource("s3-poll", cfg, retain=retain)
    src._seen["prefix/hello.txt"] = "abc123"  # pre-populate as already seen

    objects = [{"Key": "prefix/hello.txt", "ETag": '"abc123"'}]
    mock_client = _make_mock_s3_client(objects, b"Hello from S3!")

    with patch("astrocyte_ingestion_s3.source._make_client", return_value=mock_client):
        await src._poll_once()

    assert len(retained) == 0


@pytest.mark.asyncio
async def test_poll_once_skips_directory_markers() -> None:
    retained: list[str] = []

    async def retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
        retained.append(text)
        return RetainResult(stored=True, memory_id="m1")

    cfg = _make_cfg()
    src = S3PollIngestSource("s3-poll", cfg, retain=retain)

    objects = [{"Key": "prefix/", "ETag": '"dir"'}]
    mock_client = _make_mock_s3_client(objects, b"")

    with patch("astrocyte_ingestion_s3.source._make_client", return_value=mock_client):
        await src._poll_once()

    assert len(retained) == 0


# ---------------------------------------------------------------------------
# S3WebhookIngestSource — lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_start_stop() -> None:
    src = S3WebhookIngestSource("s3-wh", _webhook_cfg(), retain=_noop_retain)
    await src.start()
    assert src._running is True
    health = await src.health_check()
    assert health.healthy is True
    await src.stop()
    assert src._running is False


@pytest.mark.asyncio
async def test_webhook_health_stopped() -> None:
    src = S3WebhookIngestSource("s3-wh", _webhook_cfg(), retain=_noop_retain)
    health = await src.health_check()
    assert health.healthy is False


# ---------------------------------------------------------------------------
# S3WebhookIngestSource — handle_webhook payload parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_handles_object_created() -> None:
    retained: list[tuple[str, str]] = []

    async def retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
        retained.append((text, bank_id))
        return RetainResult(stored=True, memory_id="m1")

    src = S3WebhookIngestSource("s3-wh", _webhook_cfg(), retain=retain)
    await src.start()

    payload = {
        "Records": [
            {
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "my-bucket"},
                    "object": {"key": "docs/hello.txt", "eTag": "abc"},
                },
            }
        ]
    }

    body = b"Hello from Garage!"
    mock_client = _make_mock_s3_client([], body)  # paginator not used by webhook

    with patch("astrocyte_ingestion_s3.source._make_client", return_value=mock_client):
        # We patch _ingest_object to avoid full aiobotocore setup
        src._last_error = None

        async def fake_ingest(bucket: str, key: str) -> bool:
            retained.append(("fake-text", "bank-a"))
            return True

        src._ingest_object = fake_ingest  # type: ignore[method-assign]
        result = await src.handle_webhook(json.dumps(payload).encode(), {})

    assert result["processed"] == 1
    assert result["skipped"] == 0
    await src.stop()


@pytest.mark.asyncio
async def test_webhook_skips_non_create_events() -> None:
    src = S3WebhookIngestSource("s3-wh", _webhook_cfg(), retain=_noop_retain)
    await src.start()

    payload = {
        "Records": [
            {
                "eventName": "ObjectRemoved:Delete",
                "s3": {
                    "bucket": {"name": "my-bucket"},
                    "object": {"key": "docs/old.txt"},
                },
            }
        ]
    }
    result = await src.handle_webhook(json.dumps(payload).encode(), {})
    assert result["processed"] == 0
    assert result["skipped"] == 1
    await src.stop()


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_json() -> None:
    src = S3WebhookIngestSource("s3-wh", _webhook_cfg(), retain=_noop_retain)
    await src.start()

    with pytest.raises(IngestError, match="invalid JSON"):
        await src.handle_webhook(b"not json {{", {})
    await src.stop()


@pytest.mark.asyncio
async def test_webhook_rejects_missing_records() -> None:
    src = S3WebhookIngestSource("s3-wh", _webhook_cfg(), retain=_noop_retain)
    await src.start()

    with pytest.raises(IngestError, match="Records"):
        await src.handle_webhook(json.dumps({"foo": "bar"}).encode(), {})
    await src.stop()


@pytest.mark.asyncio
async def test_webhook_skips_record_missing_bucket_or_key() -> None:
    src = S3WebhookIngestSource("s3-wh", _webhook_cfg(), retain=_noop_retain)
    await src.start()

    payload = {
        "Records": [
            {
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {}, "object": {}},  # missing name/key
            }
        ]
    }
    result = await src.handle_webhook(json.dumps(payload).encode(), {})
    assert result["processed"] == 0
    assert result["skipped"] == 1
    await src.stop()
