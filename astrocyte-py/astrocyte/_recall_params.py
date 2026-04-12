"""Typed recall parameters — replaces fragile dict[str, Any] callback parameter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from astrocyte.types import MemoryHit


@dataclass(frozen=True)
class RecallParams:
    """Typed bundle of optional recall parameters passed through internal helpers."""

    external_context: list[MemoryHit] | None = None
    fact_types: list[str] | None = None
    time_range: tuple[datetime, datetime] | None = None
    include_sources: bool = False
    layer_weights: dict[str, float] | None = None
    detail_level: str | None = None
