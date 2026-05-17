"""Pluggable backend for background task execution.

Adopted from Hindsight (``hindsight_api/engine/task_backend.py``) but
scoped down to what Astrocyte uses today. Hindsight's full implementation
includes a Postgres-LISTEN/NOTIFY broker for distributed worker pools;
we don't have that architecture yet (planned: daemon/worker split, future
sprint). For now we ship two backends:

  - ``SyncTaskBackend``: executes tasks immediately, inline. For tests
    and CLI/embedded use.
  - ``AsyncFireAndTrackBackend``: ``asyncio.create_task`` per submission,
    tracks running tasks, awaits them on ``shutdown()``. Matches the
    current ``orchestrator._run_consolidation()`` and
    ``compile_trigger._worker()`` patterns but gives them a uniform
    interface and a shutdown-coordinated drain.

Both implement the same ``TaskBackend`` interface so a future
``PostgresBrokerTaskBackend`` can drop in without changing callers.

Usage pattern:

    backend = AsyncFireAndTrackBackend()
    backend.set_executor(run_task)        # async fn (task_dict) -> None
    await backend.initialize()
    ...
    await backend.submit({"type": "consolidation", "bank_id": "..."})
    ...
    await backend.shutdown()              # drains in-flight tasks

Why an executor callback + dict, not direct coroutine submission: it lets
the same task representation move across process boundaries later (the
"broker" backend would persist the dict and a worker would dequeue it).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


Executor = Callable[[dict[str, Any]], Awaitable[None]]


# ─── abstract base ────────────────────────────────────────────────────


class TaskBackend(ABC):
    """Abstract base for task execution backends.

    Tasks are pure dicts so backends can serialize/persist them. The
    executor (callback) routes each dict to the appropriate handler.
    """

    def __init__(self) -> None:
        self._executor: Executor | None = None
        self._initialized = False

    def set_executor(self, executor: Executor) -> None:
        """Register the callback that runs each submitted task."""
        self._executor = executor

    @abstractmethod
    async def initialize(self) -> None:
        """Bring the backend up (e.g. open DB connection, start poller)."""

    @abstractmethod
    async def submit(self, task: dict[str, Any]) -> None:
        """Submit a task for execution.

        Backends may execute immediately (sync), schedule (async fire),
        or persist (broker). All return promptly; the caller never
        awaits the actual task completion via this method.
        """

    @abstractmethod
    async def shutdown(self) -> None:
        """Drain in-flight tasks and release resources."""

    async def _run(self, task: dict[str, Any]) -> None:
        """Execute via the registered executor. Errors are logged + swallowed."""
        if self._executor is None:
            task_type = task.get("type", "<no type>")
            logger.warning("task_backend: no executor for task type=%s; dropping", task_type)
            return
        try:
            await self._executor(task)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "task_backend: executor failed for type=%s: %s",
                task.get("type", "<no type>"),
                exc,
            )


# ─── synchronous backend (tests / CLI) ────────────────────────────────


class SyncTaskBackend(TaskBackend):
    """Executes tasks inline. Useful for tests and CLI / embedded use."""

    async def initialize(self) -> None:
        self._initialized = True

    async def submit(self, task: dict[str, Any]) -> None:
        await self._run(task)

    async def shutdown(self) -> None:
        self._initialized = False


# ─── async fire-and-track backend (current "background work" pattern) ─


class AsyncFireAndTrackBackend(TaskBackend):
    """Spawns ``asyncio.create_task`` per submit and tracks the resulting tasks.

    Closes a gap in the raw ``asyncio.create_task`` pattern: nothing
    awaits the spawned task, so failures are silently swallowed and the
    event loop may shut down with in-flight work. This backend tracks
    every submitted task; on ``shutdown()`` it gathers them with a
    bounded timeout so the shutdown is observable.

    Failures inside individual tasks are logged via ``_run`` but never
    raise to the spawner — matches the existing fire-and-forget pattern.
    """

    def __init__(self, *, shutdown_timeout_seconds: float = 30.0) -> None:
        super().__init__()
        self._inflight: set[asyncio.Task[None]] = set()
        self._shutdown_timeout = shutdown_timeout_seconds

    async def initialize(self) -> None:
        self._initialized = True

    async def submit(self, task: dict[str, Any]) -> None:
        if not self._initialized:
            logger.warning("task_backend: submit before initialize for type=%s", task.get("type", "<no type>"))
        try:
            t = asyncio.create_task(self._run(task), name=f"astrocyte.task.{task.get('type', 'unknown')}")
        except RuntimeError:
            # No running loop (rare); fall through to sync execution
            logger.debug("task_backend: no event loop; running inline")
            await self._run(task)
            return
        self._inflight.add(t)
        t.add_done_callback(self._inflight.discard)

    async def shutdown(self) -> None:
        self._initialized = False
        if not self._inflight:
            return
        pending = list(self._inflight)
        logger.info("task_backend: draining %d in-flight tasks", len(pending))
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=self._shutdown_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "task_backend: shutdown timed out after %.1fs; %d tasks still running",
                self._shutdown_timeout,
                len(self._inflight),
            )

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)


# ─── default singleton ────────────────────────────────────────────────

_DEFAULT_BACKEND: TaskBackend | None = None


def get_default_task_backend() -> TaskBackend:
    """Return the process-wide default task backend.

    Defaults to ``SyncTaskBackend`` so embedded / test contexts work
    without explicit setup. Production setup should call
    ``set_default_task_backend(AsyncFireAndTrackBackend())`` or a
    future broker backend at startup.
    """
    global _DEFAULT_BACKEND
    if _DEFAULT_BACKEND is None:
        _DEFAULT_BACKEND = SyncTaskBackend()
    return _DEFAULT_BACKEND


def set_default_task_backend(backend: TaskBackend) -> None:
    global _DEFAULT_BACKEND
    _DEFAULT_BACKEND = backend
