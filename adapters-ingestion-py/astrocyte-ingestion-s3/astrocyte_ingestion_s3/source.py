"""S3/Garage :class:`~astrocyte.ingest.source.IngestSource` adapters.

Two flavours
------------
``S3PollIngestSource``
    Polls a bucket every ``interval_seconds`` seconds.  Tracks ingested
    objects by ETag so it skips unchanged files between polls.

``S3WebhookIngestSource``
    Handles Garage / AWS S3 event notification payloads posted to the
    gateway webhook endpoint.  The gateway webhook route calls
    ``source.handle_webhook(raw_body, headers)`` when the source type is
    ``"webhook"`` and the source exposes that method.

Configuration (``sources`` block in ``astrocyte.yaml``)
-------------------------------------------------------
    type: poll           # or webhook
    driver: s3
    url: https://garage.example.com   # optional; empty = AWS S3
    path: my-bucket/optional-prefix/  # bucket[/prefix]
    auth:
      access_key: AKID…
      secret_key: …
    interval_seconds: 300             # poll only
    target_bank: my-bank
    extraction_profile: document
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from astrocyte.config import SourceConfig
from astrocyte.errors import IngestError
from astrocyte.ingest.bank_resolve import resolve_ingest_bank_id
from astrocyte.ingest.logutil import log_ingest_event
from astrocyte.ingest.webhook import RetainCallable
from astrocyte.types import AstrocyteContext, HealthStatus

from astrocyte_ingestion_s3._extract import MAX_BYTES, extract_text

logger = logging.getLogger("astrocyte_ingestion_s3")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _bucket_prefix(cfg: SourceConfig) -> tuple[str, str]:
    """Return ``(bucket, prefix)`` from ``config.path``.

    ``path`` format: ``bucket`` or ``bucket/prefix/``.
    """
    p = (cfg.path or "").strip().strip("/")
    if not p:
        raise IngestError("S3 source requires path: bucket or bucket/prefix/")
    parts = p.split("/", 1)
    bucket = parts[0].strip()
    prefix = (parts[1].strip("/") + "/") if len(parts) > 1 and parts[1].strip() else ""
    if not bucket:
        raise IngestError("S3 source path must start with a bucket name")
    return bucket, prefix


def _endpoint_url(cfg: SourceConfig) -> str | None:
    """Return the endpoint URL for Garage/MinIO; ``None`` for native AWS."""
    u = (cfg.url or "").strip()
    return u if u else None


def _credentials(cfg: SourceConfig) -> tuple[str, str]:
    auth = cfg.auth or {}
    key = str(auth.get("access_key") or "").strip()
    secret = str(auth.get("secret_key") or "").strip()
    if not key or not secret:
        raise IngestError("S3 source requires auth.access_key and auth.secret_key")
    return key, secret


# ---------------------------------------------------------------------------
# Shared aiobotocore session factory
# ---------------------------------------------------------------------------


def _make_client(cfg: SourceConfig):  # type: ignore[return]  # returns async context manager
    """Return an ``aiobotocore`` S3 client (async context manager)."""
    try:
        import aiobotocore.session as aio_session  # type: ignore[import-untyped]
    except ImportError as exc:
        raise IngestError("aiobotocore is required for the S3 adapter; install astrocyte-ingestion-s3") from exc

    access_key, secret_key = _credentials(cfg)
    endpoint_url = _endpoint_url(cfg)

    session = aio_session.get_session()
    kwargs: dict[str, Any] = {
        "service_name": "s3",
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
    }
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return session.create_client(**kwargs)


# ---------------------------------------------------------------------------
# Poll source
# ---------------------------------------------------------------------------


class S3PollIngestSource:
    """Poll an S3/Garage bucket for new or changed objects."""

    def __init__(
        self,
        source_id: str,
        config: SourceConfig,
        *,
        retain: RetainCallable,
    ) -> None:
        self._source_id = source_id
        self._config = config
        self._retain = retain
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._running = False
        self._last_error: str | None = None
        # key -> etag for change detection
        self._seen: dict[str, str] = {}

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> str:
        return "poll"

    @property
    def config(self) -> SourceConfig:
        return self._config

    def _interval_s(self) -> float:
        n = self._config.interval_seconds
        if n is None or int(n) < 60:
            raise IngestError("S3 poll requires interval_seconds >= 60")
        return float(int(n))

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._last_error = None
        self._running = True
        # Validate config early (raises IngestError on bad config)
        _bucket_prefix(self._config)
        _credentials(self._config)
        self._task = asyncio.create_task(self._run_loop(), name=f"astrocyte-s3-poll-{self._source_id}")

    async def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def health_check(self) -> HealthStatus:
        if not self._running or self._task is None:
            return HealthStatus(healthy=False, message="s3 poll source stopped")
        if self._last_error:
            return HealthStatus(healthy=False, message=self._last_error)
        return HealthStatus(healthy=True, message="s3 poll loop running")

    async def _run_loop(self) -> None:
        interval = self._interval_s()
        while not self._stop.is_set():
            try:
                await self._poll_once()
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                log_ingest_event(
                    logger,
                    "s3_poll_cycle_failed",
                    source_id=self._source_id,
                    error=str(exc),
                )
                logger.exception("s3 poll failed for %s", self._source_id)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _poll_once(self) -> None:
        bucket, prefix = _bucket_prefix(self._config)
        async with _make_client(self._config) as s3:
            paginator = s3.get_paginator("list_objects_v2")
            kwargs: dict[str, Any] = {"Bucket": bucket}
            if prefix:
                kwargs["Prefix"] = prefix

            async for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents") or []:
                    key: str = obj["Key"]
                    etag: str = obj.get("ETag", "").strip('"')
                    # Skip directory markers
                    if key.endswith("/"):
                        continue
                    if self._seen.get(key) == etag:
                        continue
                    await self._ingest_object(s3, bucket, key, etag)

    async def _ingest_object(self, s3: Any, bucket: str, key: str, etag: str) -> None:
        try:
            resp = await s3.get_object(Bucket=bucket, Key=key)
            body_stream = resp["Body"]
            # Read with size cap
            chunks: list[bytes] = []
            total = 0
            async with body_stream as stream:
                async for chunk in stream:
                    total += len(chunk)
                    if total > MAX_BYTES:
                        logger.warning(
                            "s3 ingest: object too large (>%d bytes), skipping key=%s/%s",
                            MAX_BYTES, bucket, key,
                        )
                        return
                    chunks.append(chunk)
            body = b"".join(chunks)
            content_type = resp.get("ContentType")
        except Exception as exc:
            log_ingest_event(
                logger,
                "s3_get_object_failed",
                source_id=self._source_id,
                bucket=bucket,
                key=key,
                error=str(exc),
            )
            logger.warning("s3 ingest: could not fetch s3://%s/%s: %s", bucket, key, exc)
            return

        text = extract_text(key, body, content_type)
        if not text or not text.strip():
            logger.debug("s3 ingest: no text extracted from s3://%s/%s", bucket, key)
            self._seen[key] = etag
            return

        try:
            bank_id = resolve_ingest_bank_id(self._config)
        except IngestError as exc:
            logger.warning("s3 poll %s skip key=%s: %s", self._source_id, key, exc)
            return

        p = self._config.principal
        ctx = AstrocyteContext(principal=p) if p else None
        metadata: dict[str, Any] = {
            "s3": {
                "bucket": bucket,
                "key": key,
                "etag": etag,
                "content_type": content_type or "",
            }
        }

        await self._retain(
            text,
            bank_id,
            metadata=metadata,
            content_type="document",
            extraction_profile=self._config.extraction_profile,
            source=self._source_id,
            context=ctx,
        )
        self._seen[key] = etag
        log_ingest_event(
            logger,
            "s3_object_ingested",
            source_id=self._source_id,
            bucket=bucket,
            key=key,
        )


# ---------------------------------------------------------------------------
# Webhook source (Garage / AWS S3 event notifications)
# ---------------------------------------------------------------------------


class S3WebhookIngestSource:
    """Handle Garage/AWS S3 event-notification payloads.

    Garage and AWS S3 both send event notifications in the same envelope::

        {
            "Records": [
                {
                    "eventName": "ObjectCreated:Put",
                    "s3": {
                        "bucket": {"name": "my-bucket"},
                        "object": {"key": "path/to/file.pdf", "eTag": "abc123"}
                    }
                }
            ]
        }

    The gateway webhook route calls ``source.handle_webhook(raw_body, headers)``
    for sources that expose this method (detected via ``hasattr``).

    Configure in ``astrocyte.yaml``::

        sources:
          garage-docs:
            type: webhook
            driver: s3
            url: https://garage.example.com   # endpoint for fetching objects
            auth:
              access_key: …
              secret_key: …
            target_bank: my-bank
            extraction_profile: document
    """

    def __init__(
        self,
        source_id: str,
        config: SourceConfig,
        *,
        retain: RetainCallable,
    ) -> None:
        self._source_id = source_id
        self._config = config
        self._retain = retain
        self._running = False
        self._last_error: str | None = None

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> str:
        return "webhook"

    @property
    def config(self) -> SourceConfig:
        return self._config

    async def start(self) -> None:
        _credentials(self._config)
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus(healthy=False, message="s3 webhook source stopped")
        if self._last_error:
            return HealthStatus(healthy=False, message=self._last_error)
        return HealthStatus(healthy=True, message="s3 webhook ready")

    async def handle_webhook(self, raw_body: bytes, headers: dict[str, str]) -> dict[str, Any]:
        """Process an S3/Garage event notification payload.

        Returns a summary dict: ``{"processed": int, "skipped": int}``.
        Raises ``IngestError`` on malformed payloads.
        """
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise IngestError(f"S3 webhook: invalid JSON payload: {exc}") from exc

        records = payload.get("Records")
        if not isinstance(records, list):
            raise IngestError("S3 webhook: payload missing 'Records' array")

        processed = 0
        skipped = 0
        for record in records:
            if not isinstance(record, dict):
                skipped += 1
                continue
            event_name = str(record.get("eventName", ""))
            # Only handle object-creation events
            if not event_name.startswith("ObjectCreated"):
                skipped += 1
                continue
            s3_info = record.get("s3", {})
            bucket = (s3_info.get("bucket") or {}).get("name", "")
            obj = s3_info.get("object") or {}
            key = obj.get("key", "")
            if not bucket or not key:
                logger.warning("s3 webhook: missing bucket/key in record, skipping")
                skipped += 1
                continue
            ok = await self._ingest_object(bucket, key)
            if ok:
                processed += 1
            else:
                skipped += 1

        self._last_error = None
        return {"processed": processed, "skipped": skipped}

    async def _ingest_object(self, bucket: str, key: str) -> bool:
        """Fetch *key* from *bucket* and retain its text. Returns True on success."""
        try:
            async with _make_client(self._config) as s3:
                resp = await s3.get_object(Bucket=bucket, Key=key)
                content_type: str | None = resp.get("ContentType")
                body_stream = resp["Body"]
                chunks: list[bytes] = []
                total = 0
                async with body_stream as stream:
                    async for chunk in stream:
                        total += len(chunk)
                        if total > MAX_BYTES:
                            logger.warning(
                                "s3 webhook: object too large (>%d bytes), skipping key=%s/%s",
                                MAX_BYTES, bucket, key,
                            )
                            return False
                        chunks.append(chunk)
                body = b"".join(chunks)
        except Exception as exc:
            self._last_error = str(exc)
            log_ingest_event(
                logger,
                "s3_webhook_get_failed",
                source_id=self._source_id,
                bucket=bucket,
                key=key,
                error=str(exc),
            )
            logger.warning("s3 webhook: could not fetch s3://%s/%s: %s", bucket, key, exc)
            return False

        text = extract_text(key, body, content_type)
        if not text or not text.strip():
            logger.debug("s3 webhook: no text extracted from s3://%s/%s", bucket, key)
            return False

        try:
            bank_id = resolve_ingest_bank_id(self._config)
        except IngestError as exc:
            logger.warning("s3 webhook %s skip key=%s: %s", self._source_id, key, exc)
            return False

        p = self._config.principal
        ctx = AstrocyteContext(principal=p) if p else None
        metadata: dict[str, Any] = {
            "s3": {
                "bucket": bucket,
                "key": key,
                "content_type": content_type or "",
            }
        }

        await self._retain(
            text,
            bank_id,
            metadata=metadata,
            content_type="document",
            extraction_profile=self._config.extraction_profile,
            source=self._source_id,
            context=ctx,
        )
        log_ingest_event(
            logger,
            "s3_webhook_object_ingested",
            source_id=self._source_id,
            bucket=bucket,
            key=key,
        )
        return True
