"""Registry of ingest sources loaded from config (M4)."""

from __future__ import annotations

from astrocyte.config import SourceConfig
from astrocyte.ingest.source import IngestSource, WebhookIngestSource


class SourceRegistry:
    """Keeps :class:`IngestSource` instances keyed by ``sources`` config id."""

    def __init__(self) -> None:
        self._by_id: dict[str, IngestSource] = {}

    def register(self, source: IngestSource) -> None:
        self._by_id[source.source_id] = source

    def get(self, source_id: str) -> IngestSource | None:
        return self._by_id.get(source_id)

    def all_sources(self) -> list[IngestSource]:
        return list(self._by_id.values())

    async def start_all(self) -> None:
        for s in self._by_id.values():
            await s.start()

    async def stop_all(self) -> None:
        for s in self._by_id.values():
            await s.stop()

    @classmethod
    def from_sources_config(cls, sources: dict[str, SourceConfig] | None) -> SourceRegistry:
        reg = cls()
        if not sources:
            return reg
        for sid, cfg in sources.items():
            st = (cfg.type or "").strip().lower()
            if st == "webhook":
                reg.register(WebhookIngestSource(str(sid), cfg))
        return reg
