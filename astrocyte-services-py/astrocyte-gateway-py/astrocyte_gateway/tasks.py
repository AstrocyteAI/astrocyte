"""Gateway lifecycle wiring for PgQueuer memory tasks."""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from typing import Any

from astrocyte import Astrocyte
from astrocyte.pipeline.compile import CompileEngine
from astrocyte.pipeline.lint import LintEngine
from astrocyte.pipeline.pgqueuer_tasks import PgQueuerMemoryTaskQueue
from astrocyte.pipeline.tasks import MemoryTaskDispatcher, TaskHandlerContext


@dataclass
class GatewayTaskWorker:
    """Resources owned by the gateway task worker lifecycle."""

    queue: PgQueuerMemoryTaskQueue
    worker_task: asyncio.Task[None] | None = None
    connection: Any | None = None

    async def stop(self) -> None:
        await self.queue.shutdown()
        if self.worker_task is not None:
            self.worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker_task
        if self.connection is not None:
            await self.connection.close()


async def start_gateway_task_worker(brain: Astrocyte) -> GatewayTaskWorker | None:
    """Start the configured PgQueuer worker for memory tasks, if enabled."""

    config = brain.config.async_tasks
    if not config.enabled:
        return None

    queue, connection = await _build_queue(config.backend, config.dsn)
    if config.install_on_start:
        await queue.install()
    queue.register_dispatcher(_build_dispatcher(brain))

    task: asyncio.Task[None] | None = None
    if config.auto_start_worker:
        task = asyncio.create_task(
            queue.run_continuous(batch_size=config.batch_size),
            name="astrocyte.pgqueuer_worker",
        )
    return GatewayTaskWorker(queue=queue, worker_task=task, connection=connection)


async def _build_queue(
    backend: str,
    dsn: str | None,
) -> tuple[PgQueuerMemoryTaskQueue, Any | None]:
    if backend == "pgqueuer_in_memory":
        return PgQueuerMemoryTaskQueue.in_memory(), None
    if backend != "pgqueuer":
        raise ValueError(f"Unsupported async_tasks backend: {backend}")

    resolved_dsn = dsn or os.environ.get("ASTROCYTE_TASKS_DSN") or os.environ.get("DATABASE_URL")
    if not resolved_dsn:
        raise ValueError("async_tasks.backend=pgqueuer requires async_tasks.dsn or ASTROCYTE_TASKS_DSN")

    import psycopg

    connection = await psycopg.AsyncConnection.connect(resolved_dsn, autocommit=True)
    return PgQueuerMemoryTaskQueue.from_psycopg_connection(connection), connection


def _build_dispatcher(brain: Astrocyte) -> MemoryTaskDispatcher:
    pipeline = getattr(brain, "_pipeline", None)
    if pipeline is None:
        raise ValueError("async task worker requires a Tier 1 pipeline")

    wiki_store = getattr(brain, "_wiki_store", None)
    compile_engine = None
    lint_engine = None
    if wiki_store is not None:
        compile_engine = CompileEngine(
            vector_store=pipeline.vector_store,
            llm_provider=pipeline.llm_provider,
            wiki_store=wiki_store,
        )
        lint_engine = LintEngine(
            vector_store=pipeline.vector_store,
            wiki_store=wiki_store,
            llm_provider=pipeline.llm_provider,
        )

    return MemoryTaskDispatcher(
        TaskHandlerContext(
            vector_store=pipeline.vector_store,
            llm_provider=pipeline.llm_provider,
            wiki_store=wiki_store,
            graph_store=getattr(pipeline, "graph_store", None),
            compile_engine=compile_engine,
            lint_engine=lint_engine,
        )
    )
