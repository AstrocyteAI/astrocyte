"""M14.0: FSM engine driving the retain pipeline.

Pure-Python, no DSL, no Rust. States are async coroutines that take
``(ctx, services)`` and return one of:
- a state name string → next state
- :class:`Complete` → terminate successfully
- :class:`Failed` → terminate with error
- :class:`Parallel` → fan out to multiple states concurrently, then
  join at a named next state

The engine handles checkpoint between transitions, error capture, and
parallel-join semantics. State implementations live in
:mod:`astrocyte.pipeline.retain_fsm.states` and are registered via
``RetainFSM.register``.

See ``docs/_design/m13-m14-roadmap.md`` §4.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

from astrocyte.pipeline.retain_fsm.context import (
    RetainContext,
    RetainServices,
    StepLogEntry,
)

if TYPE_CHECKING:
    from astrocyte.pipeline.retain_fsm.checkpoint import Checkpoint

logger = logging.getLogger("astrocyte.pipeline.retain_fsm.engine")


# ── Transition return types ────────────────────────────────────────────


@dataclass(frozen=True)
class Complete:
    """Terminal success — drives the engine to mark ``ctx.completed_at``
    and return."""


@dataclass(frozen=True)
class Failed:
    """Terminal failure with a reason. Appended to ``ctx.errors``."""

    reason: str


@dataclass(frozen=True)
class Parallel:
    """Fan out to ``branches`` concurrently; all must complete before
    transitioning to ``join``. Each branch is run as if it were a top-
    level state (with the same context, awaited via ``asyncio.gather``).

    Branches share the context by reference — they MUST not race on the
    same field. Use this for embarrassingly-parallel work (e.g.
    extraction + entities + embeddings, which write disjoint context
    fields).
    """

    branches: tuple[str, ...]
    join: str


StateResult = "str | Complete | Failed | Parallel"
StateFunc = Callable[[RetainContext, RetainServices], Awaitable["StateResult"]]


# ── Engine ─────────────────────────────────────────────────────────────


class RetainFSM:
    """Drive a :class:`RetainContext` through registered states until
    termination.

    Usage::

        fsm = RetainFSM(services)
        fsm.register("INIT", state_init)
        fsm.register("READ", state_read)
        fsm.register("COMPLETE", state_complete)  # optional explicit
        ctx = RetainContext(bank_id="b1", source_id="s1", md_text="...")
        ctx = await fsm.run(ctx)
        assert ctx.completed_at is not None
    """

    def __init__(self, services: RetainServices) -> None:
        self.services = services
        self._registry: dict[str, StateFunc] = {}

    # ── Registration ───────────────────────────────────────────────────

    def register(self, name: str, fn: StateFunc) -> None:
        """Register a state coroutine. Subsequent registrations of the
        same name overwrite (intended for test stubbing)."""
        self._registry[name] = fn

    def registered_states(self) -> tuple[str, ...]:
        return tuple(sorted(self._registry))

    # ── Run loop ────────────────────────────────────────────────────────

    async def run(
        self,
        ctx: RetainContext,
        *,
        initial_state: str = "INIT",
        checkpoint: Checkpoint | None = None,
    ) -> RetainContext:
        """Drive ``ctx`` from ``initial_state`` to termination.

        If ``checkpoint`` is supplied, ``ctx`` is persisted after every
        state transition; on error the partial context is also persisted
        so :meth:`resume` can pick up.
        """
        current = initial_state
        while True:
            ctx.last_state = current
            fn = self._registry.get(current)
            if fn is None:
                ctx.errors.append(f"unknown state: {current!r}")
                if checkpoint is not None:
                    await checkpoint.save(ctx)
                return ctx

            entry = StepLogEntry(
                state=current,
                started_at=datetime.now(tz=timezone.utc),
            )
            ctx.step_log.append(entry)
            t0 = time.monotonic()

            try:
                result = await fn(ctx, self.services)
            except Exception as exc:  # noqa: BLE001 — state errors must surface
                entry.completed_at = datetime.now(tz=timezone.utc)
                entry.duration_ms = (time.monotonic() - t0) * 1000
                entry.error = f"{type(exc).__name__}: {exc}"
                ctx.errors.append(f"{current}: {entry.error}")
                logger.warning(
                    "retain_fsm: state %r raised %s",
                    current, entry.error,
                )
                if checkpoint is not None:
                    await checkpoint.save(ctx)
                return ctx

            entry.completed_at = datetime.now(tz=timezone.utc)
            entry.duration_ms = (time.monotonic() - t0) * 1000

            # ── Dispatch on result type ──
            if isinstance(result, Complete):
                ctx.completed_at = datetime.now(tz=timezone.utc)
                logger.info(
                    "retain_fsm: completed source=%s in %d states",
                    ctx.source_id, len(ctx.step_log),
                )
                if checkpoint is not None:
                    await checkpoint.save(ctx)
                return ctx

            if isinstance(result, Failed):
                ctx.errors.append(f"{current}: {result.reason}")
                logger.warning(
                    "retain_fsm: state %r reported Failed: %s",
                    current, result.reason,
                )
                if checkpoint is not None:
                    await checkpoint.save(ctx)
                return ctx

            if isinstance(result, Parallel):
                await self._run_parallel(ctx, result)
                if ctx.errors:
                    # A branch failed; treat as terminal.
                    if checkpoint is not None:
                        await checkpoint.save(ctx)
                    return ctx
                current = result.join
                if checkpoint is not None:
                    await checkpoint.save(ctx)
                continue

            # Must be a state-name string at this point.
            if not isinstance(result, str):
                ctx.errors.append(
                    f"{current}: state returned unsupported type "
                    f"{type(result).__name__}",
                )
                if checkpoint is not None:
                    await checkpoint.save(ctx)
                return ctx

            current = result
            if checkpoint is not None:
                await checkpoint.save(ctx)

    # ── Parallel branch runner ─────────────────────────────────────────

    async def _run_parallel(
        self,
        ctx: RetainContext,
        spec: Parallel,
    ) -> None:
        """Run ``spec.branches`` concurrently against the same context.

        Each branch is a state name registered on this FSM. Branches
        return their own ``StateResult`` but we only honour ``Complete``
        / ``Failed`` / state-name (treated as a single-step branch — no
        nested chains within a parallel block; that's deferred to a
        future engine extension if needed). For M14.0 + M14.1 the
        parallel branches all do exactly one step then join.
        """
        async def _one(branch: str) -> tuple[str, str | None]:
            fn = self._registry.get(branch)
            if fn is None:
                return branch, f"unknown parallel branch: {branch!r}"
            entry = StepLogEntry(
                state=f"parallel:{branch}",
                started_at=datetime.now(tz=timezone.utc),
            )
            ctx.step_log.append(entry)
            t0 = time.monotonic()
            try:
                result = await fn(ctx, self.services)
            except Exception as exc:  # noqa: BLE001
                entry.completed_at = datetime.now(tz=timezone.utc)
                entry.duration_ms = (time.monotonic() - t0) * 1000
                entry.error = f"{type(exc).__name__}: {exc}"
                return branch, entry.error
            entry.completed_at = datetime.now(tz=timezone.utc)
            entry.duration_ms = (time.monotonic() - t0) * 1000
            if isinstance(result, Failed):
                entry.error = result.reason
                return branch, result.reason
            # Complete / state-name / Parallel returned from a branch
            # are all treated as "branch did its work" — we ignore the
            # value because the JOIN state is fixed in the spec.
            return branch, None

        results = await asyncio.gather(*[_one(b) for b in spec.branches])
        for branch, err in results:
            if err is not None:
                ctx.errors.append(f"parallel:{branch}: {err}")

    # ── Resume ─────────────────────────────────────────────────────────

    async def resume(
        self,
        ctx: RetainContext,
        *,
        checkpoint: Checkpoint | None = None,
    ) -> RetainContext:
        """Re-enter the run loop starting from ``ctx.last_state``.

        Caller is responsible for loading ``ctx`` (typically via
        :meth:`Checkpoint.load`) before calling.
        """
        return await self.run(
            ctx, initial_state=ctx.last_state, checkpoint=checkpoint,
        )
