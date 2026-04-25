"""Benchmark run checkpoint — save and resume interrupted runs.

A ``BenchmarkCheckpoint`` is a JSON file written to
``benchmark-results/checkpoints/{benchmark}-{bank_id}.json`` during a run.
It records which sessions have been retained and which questions have been
evaluated, so a run interrupted by a laptop sleep / process kill can resume
from where it left off rather than starting over.

Lifecycle
---------
1. At run start, :func:`load_or_create` returns an existing checkpoint (resume
   mode) or a fresh one (new run).
2. During the retain phase, each session key is recorded via
   :meth:`~BenchmarkCheckpoint.record_session` after a successful
   ``brain.retain()`` call.
3. During the eval phase, each question result is recorded via
   :meth:`~BenchmarkCheckpoint.record_question` immediately after scoring.
4. On successful completion, :meth:`~BenchmarkCheckpoint.complete` deletes
   the checkpoint file (clean slate for next run).

Persistence is lazy-batched: saves happen every ``save_every`` mutations
and always on :meth:`~BenchmarkCheckpoint.complete` /
:meth:`~BenchmarkCheckpoint.save` calls, so disk I/O doesn't dominate
fast mock-provider runs.

Limitations
-----------
- In-memory providers lose retained data on process exit.  Resuming the
  eval phase is still possible (cached question scores) but the retain
  phase must re-run. :attr:`~BenchmarkCheckpoint.is_resumable` returns
  ``False`` when the provider is in-memory so callers can warn and skip
  the retain-phase optimisation.
- If the bank was cleaned (``clean_after=True``) on a *previous* successful
  run, the checkpoint file was deleted, so a fresh checkpoint is created
  for the next run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("astrocyte.eval.checkpoint")


@dataclass
class BenchmarkCheckpoint:
    """Mutable snapshot of a benchmark run's progress.

    Do not instantiate directly — use :func:`load_or_create`.
    """

    benchmark: str
    bank_id: str
    path: Path

    #: Session keys that have been successfully retained.
    retained_sessions: set[str] = field(default_factory=set)

    #: question_key → result dict for already-evaluated questions.
    evaluated_questions: dict[str, dict[str, Any]] = field(default_factory=dict)

    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    #: Whether the retained data is durable (real persistent store).
    #: False → retain phase must re-run even if sessions are checkpointed.
    is_resumable: bool = True

    _dirty: int = field(default=0, repr=False)
    _save_every: int = field(default=10, repr=False)

    # ── Retain phase ──────────────────────────────────────────────────────────

    def is_session_retained(self, session_key: str) -> bool:
        """True if this session was already retained in a previous run."""
        return self.is_resumable and session_key in self.retained_sessions

    def record_session(self, session_key: str) -> None:
        """Mark a session as retained and flush to disk if due."""
        self.retained_sessions.add(session_key)
        self._bump()

    # ── Eval phase ────────────────────────────────────────────────────────────

    def is_question_evaluated(self, question_key: str) -> bool:
        """True if this question was already scored in a previous run."""
        return question_key in self.evaluated_questions

    def get_question_result(self, question_key: str) -> dict[str, Any] | None:
        """Return the cached result for a previously-evaluated question."""
        return self.evaluated_questions.get(question_key)

    def record_question(self, question_key: str, result: dict[str, Any]) -> None:
        """Cache a question result and flush to disk if due."""
        self.evaluated_questions[question_key] = result
        self._bump()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _bump(self) -> None:
        self._dirty += 1
        if self._dirty >= self._save_every:
            self.save()

    def save(self) -> None:
        """Write the checkpoint to disk immediately."""
        self.updated_at = datetime.now(timezone.utc).isoformat()
        self._dirty = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "benchmark": self.benchmark,
            "bank_id": self.bank_id,
            "retained_sessions": sorted(self.retained_sessions),
            "evaluated_questions": self.evaluated_questions,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(self.path)  # atomic on POSIX
        log.debug(
            "Checkpoint saved: %d sessions, %d questions — %s",
            len(self.retained_sessions),
            len(self.evaluated_questions),
            self.path,
        )

    def complete(self) -> None:
        """Mark the run as successfully finished and remove the checkpoint file."""
        self.save()
        try:
            self.path.unlink()
            log.debug("Checkpoint deleted after successful completion: %s", self.path)
        except FileNotFoundError:
            pass

    # ── Summary ───────────────────────────────────────────────────────────────

    def resume_summary(self) -> str:
        """Human-readable one-liner for the resume banner."""
        return (
            f"Resuming from checkpoint — "
            f"{len(self.retained_sessions)} sessions already retained, "
            f"{len(self.evaluated_questions)} questions already scored "
            f"(started {self.started_at})"
        )


# ── Factory ───────────────────────────────────────────────────────────────────


def load_or_create(
    benchmark: str,
    bank_id: str,
    checkpoint_dir: Path,
    *,
    resume: bool,
    is_resumable: bool = True,
    save_every: int = 10,
) -> BenchmarkCheckpoint:
    """Load an existing checkpoint (if ``resume=True`` and one exists) or create a fresh one.

    Args:
        benchmark: Benchmark name, e.g. ``"longmemeval"`` or ``"locomo"``.
        bank_id: The bank_id used for this run — part of the checkpoint filename.
        checkpoint_dir: Directory to read/write checkpoint files.
        resume: If True, look for an existing checkpoint file and load it.
                If False, always start fresh (existing checkpoint is ignored).
        is_resumable: Set to False when the pipeline uses an in-memory store —
                      retain-phase skip is disabled even if session keys are
                      present in the checkpoint.
        save_every: Flush to disk after this many mutations.

    Returns:
        A :class:`BenchmarkCheckpoint` ready to use.
    """
    safe_bank = bank_id.replace("/", "_").replace(":", "_")
    path = checkpoint_dir / f"{benchmark}-{safe_bank}.json"

    if resume and path.exists():
        try:
            raw = json.loads(path.read_text())
            cp = BenchmarkCheckpoint(
                benchmark=raw.get("benchmark", benchmark),
                bank_id=raw.get("bank_id", bank_id),
                path=path,
                retained_sessions=set(raw.get("retained_sessions", [])),
                evaluated_questions=raw.get("evaluated_questions", {}),
                started_at=raw.get("started_at", datetime.now(timezone.utc).isoformat()),
                updated_at=raw.get("updated_at", datetime.now(timezone.utc).isoformat()),
                is_resumable=is_resumable,
                _save_every=save_every,
            )
            log.info("Loaded checkpoint from %s", path)
            return cp
        except Exception as exc:
            log.warning("Failed to load checkpoint %s (%s) — starting fresh", path, exc)

    return BenchmarkCheckpoint(
        benchmark=benchmark,
        bank_id=bank_id,
        path=path,
        is_resumable=is_resumable,
        _save_every=save_every,
    )


def checkpoint_dir_for(output_dir: Path) -> Path:
    """Return the standard checkpoint directory relative to an output dir."""
    return output_dir / "checkpoints"
