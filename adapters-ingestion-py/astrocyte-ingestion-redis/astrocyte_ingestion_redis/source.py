"""Redis Streams consumer (XREADGROUP). Field parsing: :func:`astrocyte.ingest.payload.parse_ingest_stream_fields`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from astrocyte.config import SourceConfig
from astrocyte.errors import IngestError
from astrocyte.ingest.bank_resolve import resolve_ingest_bank_id
from astrocyte.ingest.logutil import log_ingest_event
from astrocyte.ingest.payload import parse_ingest_stream_fields
from astrocyte.ingest.webhook import RetainCallable
from astrocyte.types import AstrocyteContext, HealthStatus

logger = logging.getLogger("astrocyte_ingestion_redis")


def _require_redis() -> Any:
    try:
        import redis.asyncio as redis_mod  # noqa: PLC0415
    except ImportError as e:
        raise ImportError(
            "Redis stream ingest requires the 'redis' package (declared by astrocyte-ingestion-redis)."
        ) from e
    return redis_mod


class RedisStreamIngestSource:
    """Consume a Redis Stream via XREADGROUP; each message is retained like webhook JSON."""

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
        self._redis: Any = None
        self._running = False
        self._last_error: str | None = None

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> str:
        return "stream"

    @property
    def config(self) -> SourceConfig:
        return self._config

    def _stream_key(self) -> str:
        t = (self._config.topic or "").strip()
        if not t:
            raise IngestError("stream source requires topic (Redis stream key)")
        return t

    def _group(self) -> str:
        g = (self._config.consumer_group or "").strip()
        if not g:
            raise IngestError("stream source requires consumer_group")
        return g

    def _consumer_name(self) -> str:
        p = (self._config.path or "").strip()
        if p:
            return p
        return f"astrocyte-{self._source_id}"

    def _redis_url(self) -> str:
        u = (self._config.url or "").strip()
        if not u:
            raise IngestError("stream source requires url (Redis connection URL)")
        return u

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._last_error = None
        self._running = True
        redis_mod = _require_redis()
        self._redis = redis_mod.from_url(self._redis_url(), decode_responses=True)
        self._task = asyncio.create_task(self._run_loop(), name=f"astrocyte-redis-stream-{self._source_id}")

    async def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                _ = await self._task
            self._task = None
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def health_check(self) -> HealthStatus:
        if not self._running or self._task is None:
            return HealthStatus(healthy=False, message="redis stream source stopped")
        if self._last_error:
            return HealthStatus(healthy=False, message=self._last_error)
        return HealthStatus(healthy=True, message="redis stream consumer running")

    async def _ensure_group(self, r: Any, stream: str, group: str) -> None:
        import redis.exceptions as redis_exc  # noqa: PLC0415

        try:
            await r.xgroup_create(stream, group, id="0", mkstream=True)
        except redis_exc.ResponseError as e:
            if "BUSYGROUP" not in str(e).upper():
                raise

    async def _run_loop(self) -> None:
        if self._redis is None:
            raise RuntimeError("Redis client not initialized")
        r = self._redis
        stream = self._stream_key()
        group = self._group()
        consumer = self._consumer_name()
        block_ms = 5000
        try:
            await self._ensure_group(r, stream, group)
        except Exception as e:
            self._last_error = str(e)
            log_ingest_event(
                logger,
                "ingest_stream_xgroup_failed",
                source_id=self._source_id,
                transport="redis",
                error=str(e),
            )
            logger.exception("redis stream xgroup_create failed for %s", self._source_id)
            return

        while not self._stop.is_set():
            try:
                resp = await r.xreadgroup(group, consumer, {stream: ">"}, count=10, block=block_ms)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = str(e)
                log_ingest_event(
                    logger,
                    "ingest_stream_read_failed",
                    source_id=self._source_id,
                    transport="redis",
                    error=str(e),
                )
                logger.exception("redis stream xreadgroup failed for %s", self._source_id)
                await asyncio.sleep(1.0)
                continue

            if not resp:
                continue

            for _sname, messages in resp:
                for msg_id, fields in messages:
                    await self._handle_one(r, stream, group, str(msg_id), fields)

    async def _handle_one(self, r: Any, stream: str, group: str, msg_id: str, fields: dict[str, str]) -> None:
        try:
            text, json_pr, content_type, metadata = parse_ingest_stream_fields(fields)
        except IngestError as e:
            logger.warning("ingest stream %s bad message %s: %s", self._source_id, msg_id, e)
            await r.xack(stream, group, msg_id)
            return

        eff_principal = json_pr or (self._config.principal or None)
        if isinstance(eff_principal, str):
            eff_principal = eff_principal.strip() or None

        try:
            bank_id = resolve_ingest_bank_id(self._config, principal=eff_principal)
        except IngestError as e:
            logger.warning("ingest stream %s bank resolve %s: %s", self._source_id, msg_id, e)
            await r.xack(stream, group, msg_id)
            return

        profile = self._config.extraction_profile
        ctx = AstrocyteContext(principal=eff_principal) if eff_principal else None

        try:
            await self._retain(
                text,
                bank_id,
                metadata=metadata,
                content_type=content_type,
                extraction_profile=profile,
                source=self._source_id,
                context=ctx,
            )
        except Exception:
            logger.exception("ingest stream %s retain failed for %s", self._source_id, msg_id)

        await r.xack(stream, group, msg_id)
