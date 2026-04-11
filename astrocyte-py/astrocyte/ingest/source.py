"""IngestSource protocol and webhook implementation (M4)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from astrocyte.config import SourceConfig
from astrocyte.types import HealthStatus


@runtime_checkable
class IngestSource(Protocol):
    """Inbound data source (webhook, stream, poll — M4+)."""

    @property
    def source_id(self) -> str: ...

    @property
    def source_type(self) -> str: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def health_check(self) -> HealthStatus: ...


class WebhookIngestSource:
    """Webhook source handle: lifecycle for registry / health; HTTP binding is adapter-level."""

    def __init__(self, source_id: str, config: SourceConfig) -> None:
        self._source_id = source_id
        self._config = config
        self._running = False

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
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if self._running:
            return HealthStatus(healthy=True, message="webhook source started")
        return HealthStatus(healthy=False, message="webhook source stopped")
