"""Owner-only permissions for the local memory store.

Memories hold conversations, so a store readable by other accounts on a
shared machine leaks them. New stores are created private (see
``astrocyte_sqlite``); stores created before that, or by hand, are reported
by ``astrocyte doctor`` and tightened by ``--fix``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .paths import data_dir

_SIDE_FILES = ("", "-wal", "-shm", "-journal")


def _owned_dir(db: Path) -> Path | None:
    """The directory too, but only Astrocyte's own: a database placed in a
    directory of the user's choosing must not change that directory."""
    parent = db.parent
    try:
        return parent if parent.resolve() == data_dir().resolve() else None
    except OSError:
        return None


def exposed(db: Path) -> list[Path]:
    """Store files (and Astrocyte's data directory) other accounts can read."""
    if os.name == "nt":
        return []  # POSIX modes don't describe Windows ACLs
    out = []
    for suffix in _SIDE_FILES:
        f = db.with_name(db.name + suffix)
        if f.exists() and f.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            out.append(f)
    d = _owned_dir(db)
    if d is not None and d.exists() and d.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        out.append(d)
    return out


def make_private(db: Path) -> list[Path]:
    """chmod what :func:`exposed` reports: files 0600, the directory 0700."""
    changed = exposed(db)
    for path in changed:
        path.chmod(0o700 if path.is_dir() else 0o600)
    return changed
