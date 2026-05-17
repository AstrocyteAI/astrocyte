"""M14.0: checkpoint persistence for the retain FSM.

After each state transition the engine optionally hands the partially-
populated ``RetainContext`` to a :class:`Checkpoint`, which persists it
to disk (or a future Postgres-backed backend). Resume reads the latest
checkpoint for a ``(bank_id, source_id)`` and re-enters the engine at
``ctx.last_state``.

Two backends for M14.0:

- :class:`FileCheckpoint` — JSON files under a configurable root.
  Default; sufficient for bench runs and unit tests.
- :class:`InMemoryCheckpoint` — dict; for tests that want resume
  semantics without touching the filesystem.

Postgres-backed checkpoint is deferred. The interface accepts arbitrary
backends; switching is a matter of subclassing :class:`Checkpoint` and
implementing ``save`` / ``load`` / ``list``.

Only fields that are JSON-serialisable round-trip cleanly. Datetime
fields are stored as ISO strings; PageIndexSection / PageIndexFact
dataclasses are NOT persisted (they're recoverable from the bank
post-resume by re-reading from the store). For M14.0 we persist only
the control-plane fields (state, errors, step_log, document_id, ids
of created/updated wikis); state implementations can mark fields as
"derived" so they're reconstructed on resume.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte.pipeline.retain_fsm.context import RetainContext

logger = logging.getLogger("astrocyte.pipeline.retain_fsm.checkpoint")


class Checkpoint(ABC):
    """Persistence backend for ``RetainContext`` snapshots."""

    @abstractmethod
    async def save(self, ctx: RetainContext) -> None:
        """Persist a snapshot of ``ctx``. Called after every state
        transition by the engine."""

    @abstractmethod
    async def load(
        self,
        bank_id: str,
        source_id: str,
    ) -> RetainContext | None:
        """Load the latest snapshot for the (bank, source) pair, or
        ``None`` if no checkpoint exists."""

    @abstractmethod
    async def delete(
        self,
        bank_id: str,
        source_id: str,
    ) -> bool:
        """Drop the checkpoint after successful completion. Returns
        ``True`` if a checkpoint existed and was deleted."""


# ── In-memory backend (tests, ephemeral runs) ───────────────────────────


class InMemoryCheckpoint(Checkpoint):
    """Dict-backed checkpoint. Loses state on process exit — used by
    tests that need round-trip semantics without filesystem coupling.
    """

    def __init__(self) -> None:
        # keyed by (bank_id, source_id) → serialised dict
        self._store: dict[tuple[str, str], dict[str, Any]] = {}

    async def save(self, ctx: RetainContext) -> None:
        self._store[(ctx.bank_id, ctx.source_id)] = _serialise(ctx)

    async def load(
        self,
        bank_id: str,
        source_id: str,
    ) -> RetainContext | None:
        raw = self._store.get((bank_id, source_id))
        if raw is None:
            return None
        return _deserialise(raw)

    async def delete(self, bank_id: str, source_id: str) -> bool:
        return self._store.pop((bank_id, source_id), None) is not None


# ── Filesystem backend (default) ────────────────────────────────────────


class FileCheckpoint(Checkpoint):
    """JSON-on-disk checkpoint. Files named
    ``<root>/<bank_id>/<source_id>.json``. Safe across process restarts;
    not safe across concurrent writes to the same source.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, bank_id: str, source_id: str) -> Path:
        # Sanitise bank_id for filesystem: replace anything that's not
        # alnum/dash/dot/underscore. Same for source_id.
        safe_bank = _safe_segment(bank_id)
        safe_src = _safe_segment(source_id)
        bank_dir = self.root / safe_bank
        bank_dir.mkdir(parents=True, exist_ok=True)
        return bank_dir / f"{safe_src}.json"

    async def save(self, ctx: RetainContext) -> None:
        path = self._path_for(ctx.bank_id, ctx.source_id)
        payload = _serialise(ctx)
        # Atomic-ish: write to tmp then rename.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(path)

    async def load(
        self,
        bank_id: str,
        source_id: str,
    ) -> RetainContext | None:
        path = self._path_for(bank_id, source_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            logger.warning(
                "checkpoint load: malformed JSON at %s: %s",
                path,
                exc,
            )
            return None
        return _deserialise(raw)

    async def delete(self, bank_id: str, source_id: str) -> bool:
        path = self._path_for(bank_id, source_id)
        if not path.exists():
            return False
        path.unlink()
        return True


# ── Serialisation helpers ──────────────────────────────────────────────


def _safe_segment(s: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)[:128] or "_"


def _serialise(ctx: RetainContext) -> dict[str, Any]:
    """Reduce a ``RetainContext`` to a JSON-serialisable dict.

    Sections / facts are NOT persisted by default — they're recoverable
    by re-reading from the bank store after resume. We persist only the
    control-plane fields and small primitive lists.
    """
    out: dict[str, Any] = {
        "schema_version": 1,
        "bank_id": ctx.bank_id,
        "source_id": ctx.source_id,
        "md_text_len": len(ctx.md_text),  # checkpoint avoids storing the full text
        "reference_date": _iso(ctx.reference_date),
        "document_id": ctx.document_id,
        "entities": list(ctx.entities),
        "wikis_created": list(ctx.wikis_created),
        "wikis_updated": list(ctx.wikis_updated),
        "supersedes_edges": [list(e) for e in ctx.supersedes_edges],
        "last_state": ctx.last_state,
        "step_log": [
            {
                "state": e.state,
                "started_at": _iso(e.started_at),
                "completed_at": _iso(e.completed_at),
                "duration_ms": e.duration_ms,
                "error": e.error,
                "notes": e.notes,
            }
            for e in ctx.step_log
        ],
        "errors": list(ctx.errors),
        "started_at": _iso(ctx.started_at),
        "completed_at": _iso(ctx.completed_at),
    }
    return out


def _deserialise(raw: dict[str, Any]) -> RetainContext:
    """Reconstruct a ``RetainContext`` from a serialised dict.

    Note: ``md_text`` is NOT restored (we only stored its length).
    Resume callers must re-supply ``md_text`` from the source if it's
    needed by remaining states. Sections / facts are also NOT restored
    — they live in the bank store; states that need them must reload
    via ``store.load_sections_with_embeddings`` etc.
    """
    from astrocyte.pipeline.retain_fsm.context import (
        RetainContext,
        StepLogEntry,
    )

    ctx = RetainContext(
        bank_id=raw["bank_id"],
        source_id=raw["source_id"],
        md_text="",  # NOT persisted; caller must supply on resume
    )
    ctx.reference_date = _parse_iso(raw.get("reference_date"))
    ctx.document_id = raw.get("document_id")
    ctx.entities = list(raw.get("entities") or [])
    ctx.wikis_created = list(raw.get("wikis_created") or [])
    ctx.wikis_updated = list(raw.get("wikis_updated") or [])
    ctx.supersedes_edges = [tuple(e) for e in raw.get("supersedes_edges") or []]
    ctx.last_state = raw.get("last_state") or "INIT"
    ctx.errors = list(raw.get("errors") or [])
    ctx.started_at = _parse_iso(raw.get("started_at")) or datetime.now(
        tz=timezone.utc,
    )
    ctx.completed_at = _parse_iso(raw.get("completed_at"))
    ctx.step_log = [
        StepLogEntry(
            state=e["state"],
            started_at=_parse_iso(e.get("started_at")) or ctx.started_at,
            completed_at=_parse_iso(e.get("completed_at")),
            duration_ms=e.get("duration_ms"),
            error=e.get("error"),
            notes=e.get("notes") or {},
        )
        for e in (raw.get("step_log") or [])
    ]
    return ctx


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_iso(s: str | None) -> datetime | None:
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
