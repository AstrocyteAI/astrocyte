"""Where a memory came from, kept on its stored chunks.

``retain(source=...)`` (a URL, a file path, an agent) used to reach only the
optional source store, so on the default path every recall hit's ``source``
was ``None``. Retain now stamps it into each chunk's metadata under
:data:`SOURCE_KEY`, and every recall path reads it back with
:func:`stored_source`.
"""

from __future__ import annotations

from typing import Any

#: Metadata key holding the retain request's ``source``. Underscore-prefixed:
#: written by the pipeline, not by callers (like ``_created_at``).
SOURCE_KEY = "_source"


def stored_source(metadata: dict[str, Any] | None) -> str | None:
    """The ``source`` a stored chunk was retained with, if any."""
    value = (metadata or {}).get(SOURCE_KEY)
    return value if isinstance(value, str) and value else None
