"""Kafka :class:`~astrocyte.ingest.source.IngestSource` — ``AIOKafkaConsumer`` loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from astrocyte.config import SourceConfig
from astrocyte.errors import IngestError
from astrocyte.ingest.bank_resolve import resolve_ingest_bank_id
from astrocyte.ingest.logutil import log_ingest_event
from astrocyte.ingest.payload import parse_ingest_kafka_value
from astrocyte.ingest.webhook import RetainCallable
from astrocyte.types import AstrocyteContext, HealthStatus

logger = logging.getLogger("astrocyte_ingestion_kafka")


def _require_aiokafka() -> Any:
    try:
        import aiokafka  # noqa: PLC0415
    except ImportError as e:
        raise ImportError(
            "Kafka stream ingest requires aiokafka (declared by astrocyte-ingestion-kafka). "
            "Install: pip install astrocyte-ingestion-kafka or pip install 'astrocyte[stream]'."
        ) from e
    return aiokafka


class KafkaStreamIngestSource:
    """Consume a Kafka topic; message **values** are JSON (webhook-shaped) or plain UTF-8 text."""

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

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> str:
        return "stream"

    @property
    def config(self) -> SourceConfig:
        return self._config

    def _topic(self) -> str:
        t = (self._config.topic or "").strip()
        if not t:
            raise IngestError("stream source requires topic (Kafka topic)")
        return t

    def _group(self) -> str:
        g = (self._config.consumer_group or "").strip()
        if not g:
            raise IngestError("stream source requires consumer_group")
        return g

    def _bootstrap_servers(self) -> str:
        u = (self._config.url or "").strip()
        if not u:
            raise IngestError("stream source requires url (Kafka bootstrap servers)")
        return u

    def _client_id(self) -> str:
        p = (self._config.path or "").strip()
        if p:
            return p
        return f"astrocyte-{self._source_id}"

    def _auto_offset_reset(self) -> str:
        auth = self._config.auth or {}
        raw = auth.get("auto_offset_reset") or auth.get("kafka_auto_offset_reset") or "earliest"
        s = str(raw).strip().lower()
        if s in ("earliest", "latest"):
            return s
        return "earliest"

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._last_error = None
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name=f"astrocyte-kafka-stream-{self._source_id}")

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
            return HealthStatus(healthy=False, message="kafka stream source stopped")
        if self._last_error:
            return HealthStatus(healthy=False, message=self._last_error)
        return HealthStatus(healthy=True, message="kafka consumer running")

    async def _run_loop(self) -> None:
        aiokafka = _require_aiokafka()
        topic = self._topic()
        consumer = aiokafka.AIOKafkaConsumer(
            topic,
            bootstrap_servers=self._bootstrap_servers(),
            group_id=self._group(),
            client_id=self._client_id(),
            enable_auto_commit=True,
            auto_offset_reset=self._auto_offset_reset(),
        )
        await consumer.start()
        try:
            async for msg in consumer:
                if self._stop.is_set():
                    break
                await self._handle_record(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._last_error = str(e)
            log_ingest_event(
                logger,
                "ingest_stream_consumer_failed",
                source_id=self._source_id,
                transport="kafka",
                error=str(e),
            )
            logger.exception("kafka consumer loop failed for %s", self._source_id)
        finally:
            await consumer.stop()

    async def _handle_record(self, msg: Any) -> None:
        try:
            text, json_pr, content_type, metadata = parse_ingest_kafka_value(msg.value)
        except IngestError as e:
            logger.warning("ingest kafka %s bad message at %s-%s: %s", self._source_id, msg.topic, msg.offset, e)
            return

        eff_principal = json_pr or (self._config.principal or None)
        if isinstance(eff_principal, str):
            eff_principal = eff_principal.strip() or None

        try:
            bank_id = resolve_ingest_bank_id(self._config, principal=eff_principal)
        except IngestError as e:
            logger.warning("ingest kafka %s bank resolve %s-%s: %s", self._source_id, msg.topic, msg.offset, e)
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
            logger.exception("ingest kafka %s retain failed for %s-%s", self._source_id, msg.topic, msg.offset)
