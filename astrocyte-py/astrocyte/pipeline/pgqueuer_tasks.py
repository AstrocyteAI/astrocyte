"""PgQueuer-backed worker integration for memory tasks.

PgQueuer owns the PostgreSQL queue mechanics (``FOR UPDATE SKIP LOCKED``,
``LISTEN/NOTIFY``, retries, scheduling). Astrocyte keeps the memory semantics in
``MemoryTaskDispatcher`` so task handlers remain backend-agnostic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from astrocyte.pipeline.tasks import (
    ANALYZE_BENCHMARK_FAILURES,
    COMPILE_BANK,
    COMPILE_PERSONA_PAGE,
    INDEX_WIKI_PAGE_VECTOR,
    LINT_WIKI_PAGE,
    NORMALIZE_TEMPORAL_FACTS,
    PROJECT_ENTITY_EDGES,
    MemoryTask,
    MemoryTaskDispatcher,
)

if TYPE_CHECKING:
    from pgqueuer import Job, PgQueuer
    from psycopg import AsyncConnection

TASK_ENTRYPOINTS = (
    COMPILE_BANK,
    COMPILE_PERSONA_PAGE,
    INDEX_WIKI_PAGE_VECTOR,
    PROJECT_ENTITY_EDGES,
    NORMALIZE_TEMPORAL_FACTS,
    LINT_WIKI_PAGE,
    ANALYZE_BENCHMARK_FAILURES,
)


class PgQueuerMemoryTaskQueue:
    """Adapter that enqueues and runs Astrocyte ``MemoryTask`` jobs via PgQueuer."""

    def __init__(self, pgq: PgQueuer) -> None:
        self.pgq = pgq

    @classmethod
    def in_memory(cls) -> PgQueuerMemoryTaskQueue:
        """Create a PgQueuer in-memory queue for tests."""

        from pgqueuer import PgQueuer

        return cls(PgQueuer.in_memory())

    @classmethod
    def from_psycopg_connection(cls, connection: AsyncConnection[Any]) -> PgQueuerMemoryTaskQueue:
        """Create a queue backed by a psycopg async Postgres connection."""

        from pgqueuer import PgQueuer

        return cls(PgQueuer.from_psycopg_connection(connection))

    async def install(self) -> None:
        """Install PgQueuer database objects when using a Postgres connection."""

        try:
            await self._queries().install()
        except Exception as exc:
            if "already exists" not in str(exc):
                raise

    async def clear(self) -> None:
        """Clear queued/logged work for the Astrocyte task entrypoints."""

        queries = self._queries()
        await queries.clear_queue(list(TASK_ENTRYPOINTS))
        await queries.clear_queue_log(list(TASK_ENTRYPOINTS))

    async def enqueue(self, task: MemoryTask, *, priority: int = 0) -> str:
        """Enqueue a memory task and return the PgQueuer job id."""

        job_ids = await self._queries().enqueue(
            task.task_type,
            _encode_task(task),
            priority=priority,
            execute_after=_delay_from_now(task.run_after),
            dedupe_key=task.idempotency_key,
        )
        return str(job_ids[0])

    def register_dispatcher(self, dispatcher: MemoryTaskDispatcher) -> None:
        """Register one PgQueuer entrypoint for each Astrocyte task type."""

        for task_type in TASK_ENTRYPOINTS:
            self._register_task_type(task_type, dispatcher)

    async def run_drain(
        self,
        *,
        batch_size: int = 10,
        max_concurrent_tasks: int | None = None,
    ) -> None:
        """Process currently queued jobs and return when the queue is drained.

        Args:
            batch_size: How many jobs PgQueuer dequeues per round.
            max_concurrent_tasks: Hard ceiling on simultaneously-running task
                handlers. ``None`` falls back to PgQueuer's unbounded default
                (``sys.maxsize``), which can exhaust downstream connection
                pools when handlers do per-task DB I/O. Recommend setting
                to ~2x ``batch_size`` (PgQueuer's enforced minimum) plus a
                small margin — e.g. ``20`` for ``batch_size=10``.
        """

        from pgqueuer.types import QueueExecutionMode

        await self.pgq.run(
            batch_size=batch_size,
            mode=QueueExecutionMode.drain,
            max_concurrent_tasks=max_concurrent_tasks,
        )

    async def run_continuous(
        self,
        *,
        batch_size: int = 10,
        max_concurrent_tasks: int | None = None,
    ) -> None:
        """Run the PgQueuer worker until shutdown is requested.

        ``max_concurrent_tasks`` bounds in-flight handler concurrency. Without
        a ceiling, PgQueuer dispatches as many tasks as the queue contains,
        which can saturate downstream connection pools (pgvector / AGE) when
        handlers do per-task DB I/O. See :meth:`run_drain` for sizing notes.
        """

        await self.pgq.run(
            batch_size=batch_size,
            max_concurrent_tasks=max_concurrent_tasks,
        )

    async def shutdown(self) -> None:
        """Stop PgQueuer listeners/workers."""

        shutdown = self.pgq.shutdown
        if callable(shutdown):
            result = shutdown()
            if result is not None:
                _ = await result  # drive the shutdown coroutine; return value unused
        else:
            shutdown.set()

    def _register_task_type(
        self,
        task_type: str,
        dispatcher: MemoryTaskDispatcher,
    ) -> None:
        @self.pgq.entrypoint(task_type)
        async def _handle(job: Job) -> None:
            task = task_from_pgqueuer_payload(job.payload, fallback_task_type=task_type)
            await dispatcher.run(task)

    def _queries(self) -> Any:
        return self.pgq.queries or self.pgq.qm.queries


def _encode_task(task: MemoryTask) -> bytes:
    return json.dumps(task_to_pgqueuer_payload(task), sort_keys=True).encode("utf-8")


def task_to_pgqueuer_payload(task: MemoryTask) -> dict[str, Any]:
    """Return the stable JSON payload shape stored in PgQueuer jobs."""

    return {
        "id": task.id,
        "task_type": task.task_type,
        "bank_id": task.bank_id,
        "payload": task.payload,
        "idempotency_key": task.idempotency_key,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "run_after": task.run_after.isoformat(),
        "created_at": task.created_at.isoformat(),
    }


def task_from_pgqueuer_payload(payload: bytes | None, *, fallback_task_type: str) -> MemoryTask:
    """Decode the stable PgQueuer JSON payload shape into a MemoryTask."""

    if not payload:
        raise ValueError("PgQueuer job payload is required for Astrocyte memory tasks")
    raw = json.loads(payload.decode("utf-8"))
    return MemoryTask(
        id=str(raw.get("id") or ""),
        task_type=str(raw.get("task_type") or fallback_task_type),
        bank_id=str(raw["bank_id"]),
        payload=dict(raw.get("payload") or {}),
        idempotency_key=raw.get("idempotency_key"),
        attempts=int(raw.get("attempts") or 0),
        max_attempts=int(raw.get("max_attempts") or 5),
        run_after=_parse_dt(raw.get("run_after")) or datetime.now(UTC),
        created_at=_parse_dt(raw.get("created_at")) or datetime.now(UTC),
    )


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _delay_from_now(run_after: datetime) -> timedelta | None:
    delay = run_after - datetime.now(UTC)
    if delay.total_seconds() <= 0:
        return None
    return delay
