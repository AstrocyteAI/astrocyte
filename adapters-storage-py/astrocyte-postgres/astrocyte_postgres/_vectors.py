"""Decode a loaded pgvector ``vector`` column into a plain ``list[float]``.

The Python value psycopg hands back for a ``vector`` column depends on the
installed pgvector-python version and on whether ``register_vector_async``
ran on that connection:

- pgvector 0.4.x, registered: ``numpy.ndarray`` (float32 elements)
- pgvector 0.5.x, registered: ``pgvector.Vector`` — *not* iterable; exposes
  ``to_list()``. ``list(value)`` raises ``TypeError``.
- not registered (e.g. the pool's ``configure`` ran before the extension
  existed, or the store never registers): the text form ``"[0.1,0.2,...]"``.
  ``list(value)`` silently yields characters.

``astrocyte-postgres`` declares ``pgvector>=0.4`` with no upper bound, so
every read path must go through :func:`parse_pgvector` rather than
``list(...)``.
"""

from __future__ import annotations

from typing import Any


def parse_pgvector(raw: Any) -> list[float] | None:
    """Return ``raw`` as ``list[float]``; ``None`` passes through."""
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip().lstrip("[").rstrip("]")
        if not s:
            return []
        return [float(p) for p in s.split(",") if p.strip()]
    if hasattr(raw, "to_list"):  # pgvector.Vector (0.5+)
        return [float(x) for x in raw.to_list()]
    if hasattr(raw, "tolist"):  # numpy.ndarray (pgvector 0.4 registered path)
        return [float(x) for x in raw.tolist()]
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    # Unknown shape — fail loudly so a future pgvector change surfaces here.
    raise TypeError(f"parse_pgvector: unsupported {type(raw).__name__}")
