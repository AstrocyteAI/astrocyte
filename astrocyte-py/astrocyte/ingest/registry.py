"""Registry of ingest sources loaded from config (M4)."""

from __future__ import annotations

from astrocyte._discovery import discover_entry_points, resolve_provider
from astrocyte.config import SourceConfig
from astrocyte.errors import ConfigError
from astrocyte.ingest.source import IngestSource, WebhookIngestSource
from astrocyte.ingest.webhook import RetainCallable


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
    def from_sources_config(
        cls,
        sources: dict[str, SourceConfig] | None,
        *,
        retain: RetainCallable | None = None,
    ) -> SourceRegistry:
        reg = cls()
        if not sources:
            return reg
        for sid, cfg in sources.items():
            st = (cfg.type or "").strip().lower()
            if st == "webhook":
                reg.register(WebhookIngestSource(str(sid), cfg))
            elif st in ("poll", "api_poll"):
                driver = (cfg.driver or "").strip().lower()
                if retain is None:
                    raise ConfigError(
                        f"sources.{sid}: type poll requires retain=... "
                        "(use astrocyte.ingest.runtime.retain_callable_for_astrocyte(astrocyte))"
                    )
                try:
                    source_cls = resolve_provider(driver, "ingest_poll_drivers")
                except LookupError as e:
                    avail = sorted(discover_entry_points("ingest_poll_drivers").keys())
                    hint = ", ".join(avail) if avail else "none"
                    raise ConfigError(
                        f"sources.{sid}: poll driver {driver!r} is not installed or unknown. "
                        f"Installed drivers: {hint}. "
                        "For GitHub, install e.g. pip install astrocyte-ingestion-github or pip install 'astrocyte[poll]'."
                    ) from e
                except Exception as e:
                    raise ConfigError(
                        f"sources.{sid}: failed to load poll driver {driver!r}: {e}"
                    ) from e
                reg.register(source_cls(str(sid), cfg, retain=retain))
            elif st == "stream":
                driver = (cfg.driver or "redis").strip().lower()
                if retain is None:
                    raise ConfigError(
                        f"sources.{sid}: type stream requires retain=... "
                        "(use astrocyte.ingest.runtime.retain_callable_for_astrocyte(astrocyte))"
                    )
                try:
                    source_cls = resolve_provider(driver, "ingest_stream_drivers")
                except LookupError as e:
                    avail = sorted(discover_entry_points("ingest_stream_drivers").keys())
                    hint = ", ".join(avail) if avail else "none"
                    raise ConfigError(
                        f"sources.{sid}: stream driver {driver!r} is not installed or unknown. "
                        f"Installed drivers: {hint}. "
                        "For kafka + redis, install e.g. pip install 'astrocyte[stream]'."
                    ) from e
                except Exception as e:
                    raise ConfigError(
                        f"sources.{sid}: failed to load stream driver {driver!r}: {e}"
                    ) from e
                reg.register(source_cls(str(sid), cfg, retain=retain))
        return reg
