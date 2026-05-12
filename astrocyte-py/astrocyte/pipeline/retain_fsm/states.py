"""M14.0: stub state implementations for the retain FSM.

This module is intentionally minimal — M14.0 ships only the engine
scaffold and three sentinel states (``INIT``, ``READ``, ``COMPLETE``)
so end-to-end tests can verify the scaffold drives transitions
correctly. The real extraction / compile / wiki / supersedes states
are added in M14.1 through M14.5.

Each state function is an async coroutine taking ``(ctx, services)`` and
returning one of:
- a state name string (transition to that state next)
- ``Complete()`` (terminal success)
- ``Failed(reason)`` (terminal failure)
- ``Parallel(branches, join)`` (fan out + join)

State implementations should:
- Read inputs from ``ctx`` (e.g. ``ctx.md_text``)
- Append outputs to mutable list fields on ``ctx`` (e.g. ``ctx.facts``)
- Use ``services.store`` / ``services.provider`` / etc. for I/O
- Return the next state name or terminal sentinel

The engine wraps each state in step-log tracking + error handling, so
state bodies should focus on the work, not the scaffolding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrocyte.pipeline.retain_fsm.engine import Complete, Failed

if TYPE_CHECKING:
    from astrocyte.pipeline.retain_fsm.context import RetainContext, RetainServices


async def state_init(
    ctx: RetainContext,
    services: RetainServices,  # noqa: ARG001  -- M14.1+ uses
) -> str | Failed:
    """Entry state: validate inputs and transition to READ.

    M14.0 minimal — just sanity-check we have the bare inputs needed.
    M14.1+ may add bank-state preflight (existing-doc detection,
    incremental-vs-fresh routing).
    """
    if not ctx.bank_id:
        return Failed("INIT: bank_id is required")
    if not ctx.source_id:
        return Failed("INIT: source_id is required")
    if not ctx.md_text or not ctx.md_text.strip():
        return Failed("INIT: md_text is empty")
    return "READ"


async def state_read(
    ctx: RetainContext,
    services: RetainServices,  # noqa: ARG001  -- M14.1+ uses
) -> str | Failed:
    """Parse / tokenise the source. M14.0 stub: no-op pass-through to
    COMPLETE. M14.1 will:
      - Run PageIndex md_to_tree
      - Save document + sections via services.store
      - Set ctx.document_id and populate ctx.sections
      - Transition to a parallel block (EXTRACT_FACTS + EXTRACT_ENTITIES + EMBED)
    """
    # Stub for M14.0 — exists so engine tests can verify the INIT→READ
    # transition fires. Real implementation in M14.1.
    return "COMPLETE"


async def state_complete(
    ctx: RetainContext,  # noqa: ARG001
    services: RetainServices,  # noqa: ARG001
) -> Complete:
    """Terminal success state. Exists so the engine can resolve
    ``COMPLETE`` as a registered state name (avoids special-casing the
    string in the engine — every transition target is a real state)."""
    return Complete()


# Default registry: convenience for callers that don't want to register
# each state by hand. Use :func:`register_default_states` on an FSM.
DEFAULT_STATES: dict[str, object] = {
    "INIT": state_init,
    "READ": state_read,
    "COMPLETE": state_complete,
}


def register_default_states(fsm) -> None:  # type: ignore[no-untyped-def]
    """Register the M14.0 stub states on a fresh :class:`RetainFSM`.

    Typed loosely to avoid a circular import; in practice callers pass
    a :class:`~astrocyte.pipeline.retain_fsm.engine.RetainFSM`.
    """
    for name, fn in DEFAULT_STATES.items():
        fsm.register(name, fn)
