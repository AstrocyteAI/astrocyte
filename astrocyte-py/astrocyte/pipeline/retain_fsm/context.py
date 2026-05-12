"""M14.0: shared context + services for the retain FSM.

The FSM engine drives stateful transitions over a single ``RetainContext``
per source. Every state mutates the context in place — appends to lists,
sets output fields — never replaces it. The context is also the unit of
checkpoint persistence: serialise it between transitions, deserialise to
resume.

``RetainServices`` bundles the dependencies states need (store, provider,
config). Passed alongside ctx into every state coroutine. Mirrors the
pattern Pydantic / FastAPI uses for dependency-injected request handlers.

See ``docs/_design/m13-m14-roadmap.md`` §4 for the architectural shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider, MentalModelStore, PageIndexStore
    from astrocyte.types import PageIndexFact, PageIndexSection


@dataclass
class StepLogEntry:
    """One entry in the per-source audit trail. Karpathy's log.md is
    materialised from these on M14.5; for M14.0+ they're an in-memory
    debug aid and the basis for `RetainContext.duration_ms_by_state`.
    """

    state: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: float | None = None
    error: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetainContext:
    """All state for one source through the retain FSM.

    Inputs are populated by the caller before ``RetainFSM.run`` starts.
    Outputs are populated by states as the pipeline progresses.
    """

    # ── Inputs ──────────────────────────────────────────────────────────
    bank_id: str
    source_id: str
    md_text: str
    reference_date: datetime | None = None

    # ── Outputs (set by states as the pipeline progresses) ──────────────
    document_id: str | None = None
    sections: list[PageIndexSection] = field(default_factory=list)
    facts: list[PageIndexFact] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    wikis_created: list[str] = field(default_factory=list)
    wikis_updated: list[str] = field(default_factory=list)
    supersedes_edges: list[tuple[str, str]] = field(default_factory=list)

    # ── Control / observability ─────────────────────────────────────────
    last_state: str = "INIT"
    step_log: list[StepLogEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )
    completed_at: datetime | None = None

    def duration_ms_by_state(self) -> dict[str, float]:
        """Aggregate per-state wall time from the step log. Used by tests
        and the M14.5 log.md emitter; safe to call mid-run."""
        out: dict[str, float] = {}
        for entry in self.step_log:
            if entry.duration_ms is None:
                continue
            out[entry.state] = out.get(entry.state, 0.0) + entry.duration_ms
        return out


@dataclass
class RetainServices:
    """Dependencies states need. Constructed once at FSM init, threaded
    into every state coroutine alongside the context.

    Stores are optional so tests / smoke runs can pass a partial bundle
    (e.g. only ``provider`` + ``store``); states that need a missing
    service should fail explicitly with a clear error rather than crash
    with ``AttributeError`` deep in the call stack.
    """

    store: PageIndexStore
    provider: LLMProvider
    mental_model_store: MentalModelStore | None = None
    embedding_model: str = "text-embedding-3-small"
    extraction_model: str = "gpt-4o-mini"
