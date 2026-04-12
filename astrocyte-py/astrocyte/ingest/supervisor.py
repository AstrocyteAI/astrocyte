"""Shared lifecycle for long-running :class:`~astrocyte.ingest.source.IngestSource` instances.

One **supervisor** per process (or per async runtime) owns ``start`` / ``stop`` and optional
graceful shutdown — the same pattern for Redis Streams, Kafka, NATS, etc. Each transport
implements :class:`~astrocyte.ingest.source.IngestSource` (background read loop inside
``start()``); this module does **not** duplicate transport logic.

Standalone HTTP stacks (e.g. FastAPI gateway) should drive an :class:`IngestSupervisor` from
app lifespan. A **worker-only** process can use the same supervisor and optionally
:func:`install_shutdown_signals` so SIGTERM/SIGINT trigger :meth:`IngestSupervisor.stop`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

from astrocyte.ingest.logutil import log_ingest_event
from astrocyte.ingest.registry import SourceRegistry
from astrocyte.types import HealthStatus

logger = logging.getLogger("astrocyte.ingest.supervisor")


class IngestSupervisor:
    """Orchestrates ``SourceRegistry.start_all`` / ``stop_all`` with optional stop timeout.

    Future hooks (backpressure, concurrency limits) can attach here without changing each
    transport implementation.
    """

    def __init__(
        self,
        registry: SourceRegistry,
        *,
        stop_timeout_s: float | None = 30.0,
    ) -> None:
        self._registry = registry
        self._stop_timeout_s = stop_timeout_s
        self._started = False

    @property
    def registry(self) -> SourceRegistry:
        return self._registry

    async def start(self) -> None:
        if self._started:
            return
        sources = self._registry.all_sources()
        await self._registry.start_all()
        self._started = True
        log_ingest_event(
            logger,
            "ingest_supervisor_started",
            source_count=len(sources),
            source_ids=[s.source_id for s in sources],
        )

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            if self._stop_timeout_s is not None:
                await asyncio.wait_for(self._registry.stop_all(), timeout=self._stop_timeout_s)
            else:
                await self._registry.stop_all()
        except TimeoutError:
            log_ingest_event(
                logger,
                "ingest_supervisor_stop_timeout",
                timeout_s=self._stop_timeout_s,
            )
            logger.warning("ingest supervisor: stop_all exceeded timeout %.1fs", self._stop_timeout_s)
            raise
        finally:
            self._started = False
            log_ingest_event(logger, "ingest_supervisor_stopped")

    async def health_snapshot(self) -> list[dict[str, Any]]:
        """Best-effort health for every registered source (admin / metrics)."""
        out: list[dict[str, Any]] = []
        for src in self._registry.all_sources():
            row: dict[str, Any] = {"id": src.source_id, "type": src.source_type}
            try:
                hs = await src.health_check()
                row["healthy"] = hs.healthy
                row["message"] = hs.message
            except Exception as e:  # noqa: BLE001 — aggregate surface
                row["healthy"] = False
                row["message"] = str(e)
            out.append(row)
        return out


def merge_source_health(rows: list[dict[str, Any]]) -> HealthStatus:
    """Reduce a snapshot from :meth:`IngestSupervisor.health_snapshot` to one :class:`HealthStatus`."""
    unhealthy = [r for r in rows if r.get("healthy") is False]
    if unhealthy:
        ids = ", ".join(str(r.get("id", "?")) for r in unhealthy)
        return HealthStatus(healthy=False, message=f"unhealthy sources: {ids}")
    return HealthStatus(healthy=True, message="all ingest sources healthy")


def install_shutdown_signals(supervisor: IngestSupervisor) -> None:
    """Register SIGINT/SIGTERM to schedule :meth:`IngestSupervisor.stop` (Unix + running loop).

    Skip when embedding under uvicorn (it already handles process signals); use for **ingest-only**
    workers.
    """
    if sys.platform == "win32":
        return
    loop = asyncio.get_running_loop()

    def _schedule_stop() -> None:
        task = asyncio.create_task(supervisor.stop())
        _ = task  # fire-and-forget; process exit may follow

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _schedule_stop)
        except NotImplementedError:
            return
