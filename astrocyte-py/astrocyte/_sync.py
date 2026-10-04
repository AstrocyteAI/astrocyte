"""Team-memory sync helpers (``team-memory.md`` §8): the change-feed cursor (G3) and push limits (G2).

The cursor is opaque to clients: URL-safe base64 of the JSON object
``{"changed_at": <ISO 8601>, "id": <memory id>}`` — the ``(changed_at, id)``
position of the last change a client has seen. Stores return only entries
strictly after it, ordered by ``(changed_at, id)``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
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


# ── push (G2) ─────────────────────────────────────────────────────────────

#: Most records one ``push_records`` call accepts.
MAX_PUSH_RECORDS = 100

#: A pushed id: 8–64 of ``[A-Za-z0-9_-]``. Local stores mint 16 hex
#: characters (``uuid4().hex[:16]``); the wider set leaves room for other
#: clients' schemes while keeping ids safe in URLs, logs and SQL.
PUSH_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")

#: ``sha256:`` and 64 lowercase hex digits.
CONTENT_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")

#: Underscore-prefixed metadata keys are the system's own. A push keeps only
#: these from the client: the reader regroups a turn's chunks by
#: ``_retain_id`` / ``_chunk_index`` and dates them by ``_created_at``.
#: ``_actor`` is stamped from the authenticated caller (a client's is kept
#: only without a context, as retain does).
PUSH_SYSTEM_METADATA_KEYS = frozenset({"_created_at", "_retain_id", "_chunk_index", "_actor"})


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
