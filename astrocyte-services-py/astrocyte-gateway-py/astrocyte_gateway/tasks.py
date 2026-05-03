"""Gateway lifecycle wiring for PgQueuer memory tasks.

Multi-tenant: when the gateway is configured with a
:class:`~astrocyte.tenancy.TenantExtension` that lists multiple tenants,
:func:`start_gateway_task_worker` starts ONE worker per tenant. Each worker
holds its own pgqueuer connection bound to that tenant's schema (via
``SET search_path``), so polled jobs come from the right per-tenant queue
and dispatched handlers see the right ``_current_schema`` context.

Single-tenant deployments (``DefaultTenantExtension`` returning the default
``public`` schema) get a single worker — same shape as before, no behavior
change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from astrocyte import Astrocyte
from astrocyte.pipeline.compile import CompileEngine
from astrocyte.pipeline.lint import LintEngine
from astrocyte.pipeline.pgqueuer_tasks import PgQueuerMemoryTaskQueue
from astrocyte.pipeline.tasks import MemoryTaskDispatcher, TaskHandlerContext
from astrocyte.tenancy import (
    DefaultTenantExtension,
    Tenant,
    TenantExtension,
    use_schema,
)

_logger = logging.getLogger("astrocyte.gateway.tasks")


@dataclass
class _TenantWorker:
    """Resources for one tenant's pgqueuer worker."""

    schema: str
    queue: PgQueuerMemoryTaskQueue
    connection: Any | None = None
    worker_task: asyncio.Task[None] | None = None

    async def stop(self) -> None:
        await self.queue.shutdown()
        if self.worker_task is not None:
            self.worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                _ = await self.worker_task
        if self.connection is not None:
            await self.connection.close()


@dataclass
class GatewayTaskWorker:
    """Resources owned by the gateway task worker lifecycle.

    Holds one :class:`_TenantWorker` per tenant returned by the configured
    :class:`~astrocyte.tenancy.TenantExtension`. ``stop()`` shuts them all
    down concurrently.
    """

    tenants: list[_TenantWorker] = field(default_factory=list)

    async def stop(self) -> None:
        if not self.tenants:
            return
        await asyncio.gather(
            *(t.stop() for t in self.tenants),
            return_exceptions=True,
        )


async def start_gateway_task_worker(
    brain: Astrocyte,
    tenant_extension: TenantExtension | None = None,
) -> GatewayTaskWorker | None:
    """Start the configured PgQueuer worker(s) for memory tasks.

    Args:
        brain: The configured ``Astrocyte`` instance.
        tenant_extension: Tenant directory. When ``None``, defaults to a
            :class:`~astrocyte.tenancy.DefaultTenantExtension` bound to the
            ``public`` schema (single-tenant behavior). Custom extensions
            list multiple tenants for per-tenant worker fan-out.

    Returns:
        :class:`GatewayTaskWorker` (with one entry per tenant) or ``None``
        when async tasks are disabled in the brain config.
    """
    config = brain.config.async_tasks
    if not config.enabled:
        return None

    if tenant_extension is None:
        tenant_extension = DefaultTenantExtension()

    tenants = await tenant_extension.list_tenants()
    if not tenants:
        _logger.warning(
            "tenant_extension.list_tenants() returned no tenants; "
            "starting no async workers (was this intentional?)",
        )
        return GatewayTaskWorker()

    dispatcher = _build_dispatcher(brain)
    workers: list[_TenantWorker] = []
    for tenant in tenants:
        try:
            tenant_worker = await _start_worker_for_tenant(
                tenant=tenant,
                config=config,
                dispatcher=dispatcher,
            )
        except Exception as exc:
            _logger.exception(
                "failed to start async worker for tenant schema %r: %s",
                tenant.schema, exc,
            )
            # Roll back already-started workers so we don't leak connections.
            await asyncio.gather(*(w.stop() for w in workers), return_exceptions=True)
            raise
        workers.append(tenant_worker)
        _logger.info("started pgqueuer worker for tenant schema=%r", tenant.schema)

    return GatewayTaskWorker(tenants=workers)


async def _start_worker_for_tenant(
    *,
    tenant: Tenant,
    config: Any,
    dispatcher: MemoryTaskDispatcher,
) -> _TenantWorker:
    """Build queue + (optional) worker task for a single tenant schema."""
    queue, connection = await _build_queue(config.backend, config.dsn, tenant.schema)
    if config.install_on_start:
        # ``install`` creates pgqueuer tables in whatever schema search_path
        # points at — which we set per-tenant in ``_build_queue``.
        await queue.install()
    queue.register_dispatcher(dispatcher)

    task: asyncio.Task[None] | None = None
    if config.auto_start_worker:
        # ``use_schema`` binds ``_current_schema`` so storage adapters
        # invoked by task handlers route to this tenant's schema. The
        # ContextVar is automatically inherited by every task PgQueuer
        # spawns inside ``run`` (asyncio.create_task copies context).
        async def _run() -> None:
            with use_schema(tenant.schema):
                await queue.run_continuous(batch_size=config.batch_size)

        task = asyncio.create_task(
            _run(),
            name=f"astrocyte.pgqueuer_worker.{tenant.schema}",
        )
    return _TenantWorker(
        schema=tenant.schema,
        queue=queue,
        connection=connection,
        worker_task=task,
    )


async def _build_queue(
    backend: str,
    dsn: str | None,
    schema: str,
) -> tuple[PgQueuerMemoryTaskQueue, Any | None]:
    """Build a PgQueuer queue with the connection's search_path bound to ``schema``."""
    if backend == "pgqueuer_in_memory":
        return PgQueuerMemoryTaskQueue.in_memory(), None
    if backend != "pgqueuer":
        raise ValueError(f"Unsupported async_tasks backend: {backend}")

    resolved_dsn = dsn or os.environ.get("ASTROCYTE_TASKS_DSN") or os.environ.get("DATABASE_URL")
    if not resolved_dsn:
        raise ValueError("async_tasks.backend=pgqueuer requires async_tasks.dsn or ASTROCYTE_TASKS_DSN")

    import psycopg

    connection = await psycopg.AsyncConnection.connect(resolved_dsn, autocommit=True)
    # Pin the search_path so pgqueuer's tables (``pgqueuer``, ``pgqueuer_log``,
    # ``pgqueuer_statistics``, ``pgqueuer_schedules``) land in this tenant's
    # schema and the worker's LISTEN/NOTIFY subscribes to that schema's queue.
    # Schema name is validated by Tenant construction → fq_table validation,
    # so it's safe to interpolate.
    await connection.execute(f'SET search_path = "{schema}", public')
    # Also create the schema if missing — single-tenant deployments where
    # ``schema=public`` no-op; multi-tenant deployments may have an admin
    # workflow that hasn't run migrate.sh for this tenant yet.
    await connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
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
