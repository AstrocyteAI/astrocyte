"""Resolve target bank for configured ingest sources (M4)."""

from __future__ import annotations

from astrocyte.config import SourceConfig
from astrocyte.errors import IngestError


def resolve_ingest_bank_id(config: SourceConfig, *, principal: str | None = None) -> str:
    """Pick ``target_bank`` or expand ``target_bank_template`` (``{principal}``)."""
    if config.target_bank and str(config.target_bank).strip():
        return str(config.target_bank).strip()
    tpl = config.target_bank_template
    if tpl and str(tpl).strip():
        p = (principal or "").strip()
        if not p:
            raise IngestError("target_bank_template requires principal (in payload or argument)")
        return str(tpl).replace("{principal}", p)
    raise IngestError("source must set target_bank or target_bank_template")
