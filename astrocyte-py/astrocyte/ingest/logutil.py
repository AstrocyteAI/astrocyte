"""Structured logging helpers for ingest (poll, stream, supervisor).

When ``ASTROCYTE_LOG_FORMAT`` is ``json`` / ``1`` / ``true`` / ``yes`` (same convention as
``astrocyte-gateway`` :mod:`astrocyte_gateway.observability`), emit one JSON object per line on
the given logger at INFO. Otherwise emit a short human-readable line.

Environment is read at **call time** so tests and workers can toggle without import order issues.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any


def _json_logs_enabled() -> bool:
    return os.environ.get("ASTROCYTE_LOG_FORMAT", "").strip().lower() in ("json", "1", "true", "yes")


def log_ingest_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Log a single ingest observability event (supervisor lifecycle, rate limits, transport errors)."""
    if _json_logs_enabled():
        payload: dict[str, Any] = {"event": event, **fields}
        logger.info(json.dumps(payload, ensure_ascii=False, default=str))
        return
    parts = " ".join(f"{k}={v!r}" for k, v in fields.items())
    logger.info("%s %s", event, parts)
