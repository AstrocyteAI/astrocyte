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
    as_of: datetime | None = None  # M9: time-travel filter
    #: Reference date for resolving relative temporal phrases in the
    #: query.  Separate from ``as_of`` (which is a retained_at filter).
    #: See ``RecallRequest.query_reference_date`` for the rationale.
    query_reference_date: datetime | None = None
