"""Reading text files people edit by hand, on every platform."""

from __future__ import annotations

import locale
from pathlib import Path


def read_user_text(path: str | Path) -> str:
    """UTF-8 (with or without the BOM some Windows editors add), else the
    platform's legacy encoding. Python's default is that legacy encoding on
    Windows (cp1252), so files written there before reads were made UTF-8,
    or saved by an editor that still uses it, keep loading."""
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")
