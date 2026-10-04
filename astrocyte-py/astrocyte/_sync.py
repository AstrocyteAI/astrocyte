"""Team-memory sync helpers: the change-feed cursor (``team-memory.md`` §8, G3).

The cursor is opaque to clients: URL-safe base64 of the JSON object
``{"changed_at": <ISO 8601>, "id": <memory id>}`` — the ``(changed_at, id)``
position of the last change a client has seen. Stores return only entries
strictly after it, ordered by ``(changed_at, id)``.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone

from astrocyte.errors import InvalidCursor

#: Most changes one ``list_changes`` call returns.
MAX_CHANGES_LIMIT = 1000


def encode_cursor(changed_at: datetime, memory_id: str) -> str:
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=timezone.utc)
    payload = json.dumps(
        {"changed_at": changed_at.astimezone(timezone.utc).isoformat(), "id": memory_id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """``(changed_at, id)`` from a cursor; :class:`InvalidCursor` if it isn't one."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw.decode("utf-8"))
        changed_at = datetime.fromisoformat(data["changed_at"])
        memory_id = data["id"]
    except (binascii.Error, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise InvalidCursor() from exc
    if not isinstance(memory_id, str) or not memory_id or changed_at.tzinfo is None:
        raise InvalidCursor()
    return changed_at, memory_id
