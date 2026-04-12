"""Shared validation helpers — used by _astrocyte.py and _policy.py."""

from __future__ import annotations

import re

from astrocyte.errors import ConfigError

_BANK_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:@\-]{0,254}$")


def validate_bank_id(bank_id: str) -> None:
    """Validate bank_id format: 1–255 chars, alphanumeric start, safe characters only."""
    if not bank_id or not _BANK_ID_RE.match(bank_id):
        raise ConfigError(
            f"Invalid bank_id {bank_id!r}: must be 1–255 characters, "
            "start with alphanumeric, and contain only [a-zA-Z0-9._:@-]"
        )
