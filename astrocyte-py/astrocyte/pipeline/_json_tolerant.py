"""Tolerant JSON parsing for LLM extraction outputs.

gpt-4o-mini occasionally wraps its JSON in markdown fences or sprinkles
prose around the object despite ``response_format={"type": "json_object"}``
being set. The strict ``json.loads`` path then fails and the caller
silently drops the section's facts / entities. This module gives the
caller a layered fallback before giving up:

1. ``json.loads`` straight up — fast path, the common case.
2. Strip a ``` ```json ``` / ``` ``` ``` markdown fence wrapper.
3. Slice from the first ``{`` to the last ``}`` (drops surrounding prose).

Returns the parsed object on success or ``None`` on total failure. The
caller keeps its existing warn-and-return-empty fallback for ``None``.

``looks_truncated`` is a sibling heuristic: when the LLM hit its
``max_tokens`` budget mid-output, the JSON is unrecoverable and a retry
with the same budget won't help either, so the caller should skip the
retry path to avoid the latency cost.
"""

from __future__ import annotations

import json
from typing import Any


def tolerant_json_loads(text: str) -> Any | None:
    """Best-effort JSON parse. Returns ``None`` if all strategies fail."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence (possibly ```json) up to first newline.
        newline = stripped.find("\n")
        inner = stripped[newline + 1 :] if newline != -1 else stripped[3:]
        # Drop trailing fence.
        inner = inner.rstrip()
        if inner.endswith("```"):
            inner = inner[:-3].rstrip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return None


def looks_truncated(text: str) -> bool:
    """Heuristic: did the LLM run out of budget mid-output?

    Used to short-circuit the parse-failure retry path. Retrying when
    the original response was budget-truncated wastes a round-trip — the
    retry will hit the same cap. Counts are naive (don't track quoting),
    which is fine: false positives just skip an occasional retry, false
    negatives just spend an extra round-trip.
    """
    if not text:
        return False
    s = text.rstrip()
    if not s:
        return False
    opens = s.count("{") + s.count("[")
    # No braces at all → not a truncated JSON object. Probably a refusal
    # or a prose answer; the retry path may recover with a stricter
    # reminder, so don't short-circuit here.
    if opens == 0:
        return False
    last = s[-1]
    if last in (",", ":"):
        return True
    closes = s.count("}") + s.count("]")
    if opens > closes:
        return True
    if last not in "}]\"'":
        return True
    return False
