"""Log-injection sanitization helpers.

CodeQL's ``py/log-injection`` query flags any log statement that
substitutes a user-controlled value, because newline / carriage-return
characters in the value can forge fake log entries (CWE-117).

The fix is to strip CRLF (and ASCII control characters more broadly)
from user-controlled values before they reach the formatter. We do this
with a tiny ``safe()`` helper rather than reaching for a structured
logger — Astrocyte's existing log call sites are scattered and we want
the smallest possible change at each one:

    logger.info("retain bank=%s", safe(bank_id))

``safe`` accepts any value (str, int, UUID, dict, ...). For strings, it
replaces every control character (0x00-0x1F, 0x7F) with a single space.
For non-strings it falls back to ``str(value)`` first, then sanitizes
the result. This keeps surrounding log context intact while preventing
log forgery and terminal-escape injection.

The double-pipeline (``str(value)`` then sanitize) means an attacker
controlling a ``__str__`` method on a custom class cannot bypass the
filter by returning bytes with embedded newlines.
"""

from __future__ import annotations

from typing import Any

_CONTROL_TRANSLATE = {c: 0x20 for c in range(0x20)}
_CONTROL_TRANSLATE[0x7F] = 0x20


def safe(value: Any) -> str:
    """Return a log-safe string representation of ``value``.

    Replaces ASCII control characters (``\\x00``-``\\x1F`` and ``\\x7F``)
    with a single space. Intended for substitution into log-format
    arguments where the value originates from user input — request
    bodies, metadata dicts, query parameters, etc.

    >>> safe("hello\\nworld")
    'hello world'
    >>> safe("bank-42")
    'bank-42'
    >>> safe(None)
    'None'
    """
    return str(value).translate(_CONTROL_TRANSLATE)
