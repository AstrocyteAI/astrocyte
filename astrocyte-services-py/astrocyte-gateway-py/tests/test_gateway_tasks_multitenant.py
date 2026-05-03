"""Tests for the multi-tenant pgqueuer worker fan-out.

Single-tenant deployments (default): one worker, byte-compatible with the
pre-multi-tenant shape. Multi-tenant deployments: one worker per tenant,
each with its own schema-bound connection.
"""

from __future__ import annotations

import asyncio
import textwrap

import pytest
from astrocyte.pipeline.pgqueuer_tasks import PgQueuerMemoryTaskQueue
from astrocyte.tenancy import (
    AuthenticationError,
    DefaultTenantExtension,
    Tenant,
    TenantContext,
    TenantExtension,
)
from astrocyte_gateway.brain import build_astrocyte
from astrocyte_gateway.tasks import start_gateway_task_worker


def _write_config(tmp_path, *, async_tasks_enabled: bool = True) -> None:
    """Drop a minimal in-memory config that enables pgqueuer_in_memory worker."""
    cfg = tmp_path / "astrocyte.yaml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            provider_tier: storage
            vector_store: in_memory
            graph_store: in_memory
            wiki_store: in_memory
            llm_provider: mock
            async_tasks:
              enabled: {str(async_tasks_enabled).lower()}
              backend: pgqueuer_in_memory
              auto_start_worker: false
            """
        ),
        encoding="utf-8",
    )
    return cfg


@pytest.fixture(autouse=True)
def reset_schema_context():
    """Each test starts with no active tenant binding."""
    from astrocyte.tenancy import _current_schema

    token = _current_schema.set(None)
    yield
    _current_schema.reset(token)


class TestSingleTenantBackwardCompat:
    """Default extension → single worker. No behavior change for existing deployments."""

    @pytest.mark.anyio
    async def test_default_extension_yields_one_worker(self, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path)
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        brain = build_astrocyte()

        worker = await start_gateway_task_worker(brain)

        assert worker is not None
        assert len(worker.tenants) == 1
        only_tenant = worker.tenants[0]
        assert only_tenant.schema == "public"
        assert isinstance(only_tenant.queue, PgQueuerMemoryTaskQueue)
        await worker.stop()

    @pytest.mark.anyio
    async def test_disabled_async_tasks_returns_none(self, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, async_tasks_enabled=False)
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        brain = build_astrocyte()
        assert await start_gateway_task_worker(brain) is None


class TestMultiTenantFanOut:
    """Custom extension listing N tenants → N workers, one per schema."""

    @pytest.mark.anyio
    async def test_three_tenants_yield_three_workers(self, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path)
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        brain = build_astrocyte()

        class ThreeTenantExtension(TenantExtension):
            async def authenticate(self, request) -> TenantContext:
                raise AuthenticationError("auth not exercised in worker test")

            async def list_tenants(self):
                return [
                    Tenant(schema="tenant_acme"),
                    Tenant(schema="tenant_globex"),
                    Tenant(schema="tenant_initech"),
                ]

        worker = await start_gateway_task_worker(brain, ThreeTenantExtension())

        assert worker is not None
        assert len(worker.tenants) == 3
        assert sorted(t.schema for t in worker.tenants) == [
            "tenant_acme", "tenant_globex", "tenant_initech",
        ]
        # Each tenant has its own queue instance — they don't share state.
        queues = {id(t.queue) for t in worker.tenants}
        assert len(queues) == 3
        await worker.stop()

    @pytest.mark.anyio
    async def test_empty_tenant_list_returns_empty_worker(self, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path)
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        brain = build_astrocyte()

        class NoTenantsExtension(TenantExtension):
            async def authenticate(self, request) -> TenantContext:
                raise AuthenticationError("not used")

            async def list_tenants(self):
                return []

        worker = await start_gateway_task_worker(brain, NoTenantsExtension())

        assert worker is not None
        assert worker.tenants == []
        # ``stop()`` on an empty worker must not raise.
        await worker.stop()

    @pytest.mark.anyio
    async def test_failure_starting_one_tenant_rolls_back_others(self, tmp_path, monkeypatch):
        """If tenant N fails to start, tenants 0..N-1 must be cleaned up.

        Otherwise we'd leak pgqueuer connections + listener tasks.
        """
        cfg = _write_config(tmp_path)
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        brain = build_astrocyte()

        # Schema name with a hyphen will fail validation in fq_table → ValueError
        # at queue setup. Use this to simulate per-tenant failure.
        class MixedExtension(TenantExtension):
            async def authenticate(self, request) -> TenantContext:
                raise AuthenticationError("not used")

            async def list_tenants(self):
                return [
                    Tenant(schema="tenant_acme"),
                    Tenant(schema="tenant_globex"),
                ]

        # Patch _build_queue to fail on the SECOND tenant.
        from astrocyte_gateway import tasks as tasks_module

        call_count = {"n": 0}
        original = tasks_module._build_queue

        async def flaky_build_queue(backend, dsn, schema):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError(f"simulated failure for {schema}")
            return await original(backend, dsn, schema)

        monkeypatch.setattr(tasks_module, "_build_queue", flaky_build_queue)

        with pytest.raises(RuntimeError, match="simulated failure"):
            await start_gateway_task_worker(brain, MixedExtension())

        # Both attempted: 1 (success), 2 (fail). Worker raised, but the first
        # tenant's queue should have been stopped during rollback. We can't
        # observe that directly without instrumentation; the contract is "no
        # exception during cleanup."


class TestSchemaContextPerWorker:
    """Each worker's run task runs inside ``use_schema(tenant.schema)``."""

    @pytest.mark.anyio
    async def test_worker_task_inherits_tenant_schema(self, tmp_path, monkeypatch):
        """When ``auto_start_worker: true``, each tenant's worker_task runs
        with its tenant schema bound. Verified by spying on the queue's
        ``run_continuous`` to capture the active schema."""
        cfg = tmp_path / "astrocyte.yaml"
        cfg.write_text(
            textwrap.dedent(
                """
                provider_tier: storage
                vector_store: in_memory
                graph_store: in_memory
                wiki_store: in_memory
                llm_provider: mock
                async_tasks:
                  enabled: true
                  backend: pgqueuer_in_memory
                  auto_start_worker: true
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        brain = build_astrocyte()

        class TwoTenantExtension(TenantExtension):
            async def authenticate(self, request) -> TenantContext:
                raise AuthenticationError("not used")

            async def list_tenants(self):
                return [Tenant(schema="tenant_a"), Tenant(schema="tenant_b")]

        captured: dict[str, list[str]] = {"schemas": []}

        # Monkeypatch the queue's run_continuous to record active schema then exit.
        from astrocyte.pipeline import pgqueuer_tasks
        from astrocyte.tenancy import get_current_schema

        async def fake_run_continuous(self, *, batch_size=10, max_concurrent_tasks=None):
            captured["schemas"].append(get_current_schema())
            # Sleep briefly so both workers get scheduled before any returns.
            await asyncio.sleep(0.05)

        monkeypatch.setattr(
            pgqueuer_tasks.PgQueuerMemoryTaskQueue,
            "run_continuous",
            fake_run_continuous,
        )

        worker = await start_gateway_task_worker(brain, TwoTenantExtension())
        assert worker is not None
        # Wait for both worker_tasks to complete (they exit after sleep).
        await asyncio.gather(*(t.worker_task for t in worker.tenants if t.worker_task))

        assert sorted(captured["schemas"]) == ["tenant_a", "tenant_b"]
        await worker.stop()


class TestDefaultExtensionContract:
    """The TenantExtension contract — ``DefaultTenantExtension`` returns
    a single tenant by default. This test pins that interface so the worker
    code can rely on ``list_tenants()`` always returning at least one entry
    in well-configured deployments."""

    @pytest.mark.anyio
    async def test_default_extension_lists_public(self):
        ext = DefaultTenantExtension()
        tenants = await ext.list_tenants()
        assert tenants == [Tenant(schema="public")]
