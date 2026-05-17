"""Tests for connection-pool budget enforcement."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrocyte.db_budget import (
    ConnectionBudgetManager,
    OperationBudget,
    get_default_budget_manager,
    set_default_budget_manager,
)

# ─── fake pools for testing ────────────────────────────────────────────


class FakePsycopgPool:
    """Mimics psycopg's AsyncConnectionPool.connection() ctx-manager shape."""

    def __init__(self) -> None:
        self.acquire_count = 0

    def connection(self):  # noqa: ANN201
        outer = self

        class _Conn:
            async def __aenter__(self_inner) -> Any:
                outer.acquire_count += 1
                return self_inner

            async def __aexit__(self_inner, *a: Any) -> None:
                pass

        return _Conn()


class FakeAsyncpgPool:
    """Mimics asyncpg's acquire()/release() shape."""

    def __init__(self) -> None:
        self.acquired: list[object] = []
        self.released: list[object] = []

    async def acquire(self) -> object:
        conn = object()
        self.acquired.append(conn)
        return conn

    async def release(self, conn: object) -> None:
        self.released.append(conn)


# ─── OperationBudget ───────────────────────────────────────────────────


class TestOperationBudget:
    def test_initializes_semaphore(self) -> None:
        b = OperationBudget(operation_id="op-1", max_connections=3)
        assert b.semaphore._value == 3
        assert b.active_count == 0
        assert b.peak_count == 0
        assert b.total_acquired == 0


# ─── ConnectionBudgetManager ──────────────────────────────────────────


class TestManager:
    @pytest.mark.asyncio
    async def test_operation_registers_and_cleans_up(self) -> None:
        mgr = ConnectionBudgetManager(default_budget=4)
        assert mgr.active_operations == 0
        async with mgr.operation() as op:
            assert mgr.active_operations == 1
            assert op.operation_id.startswith("op-")
        assert mgr.active_operations == 0

    @pytest.mark.asyncio
    async def test_explicit_operation_id(self) -> None:
        mgr = ConnectionBudgetManager()
        async with mgr.operation(operation_id="my-recall") as op:
            assert op.operation_id == "my-recall"

    @pytest.mark.asyncio
    async def test_duplicate_operation_id_raises(self) -> None:
        mgr = ConnectionBudgetManager()
        async with mgr.operation(operation_id="dupe"):
            with pytest.raises(ValueError, match="already exists"):
                async with mgr.operation(operation_id="dupe"):
                    pass

    @pytest.mark.asyncio
    async def test_invalid_budget_rejected(self) -> None:
        mgr = ConnectionBudgetManager()
        with pytest.raises(ValueError, match=">= 1"):
            async with mgr.operation(max_connections=0):
                pass

    @pytest.mark.asyncio
    async def test_default_budget_used(self) -> None:
        mgr = ConnectionBudgetManager(default_budget=2)
        async with mgr.operation() as op:
            assert op.budget.max_connections == 2


# ─── BudgetedOperation.acquire (pool-shape compatibility) ─────────────


class TestAcquirePsycopgShape:
    @pytest.mark.asyncio
    async def test_acquires_via_connection_ctx_manager(self) -> None:
        pool = FakePsycopgPool()
        mgr = ConnectionBudgetManager(default_budget=2)
        async with mgr.operation() as op, op.acquire(pool):
            assert pool.acquire_count == 1


class TestAcquireAsyncpgShape:
    @pytest.mark.asyncio
    async def test_acquires_via_acquire_release(self) -> None:
        pool = FakeAsyncpgPool()
        mgr = ConnectionBudgetManager(default_budget=2)
        async with mgr.operation() as op:
            async with op.acquire(pool):
                pass
        assert len(pool.acquired) == 1
        assert len(pool.released) == 1

    @pytest.mark.asyncio
    async def test_release_happens_on_exit(self) -> None:
        pool = FakeAsyncpgPool()
        mgr = ConnectionBudgetManager()
        async with mgr.operation() as op:
            async with op.acquire(pool):
                assert len(pool.acquired) == 1
                assert len(pool.released) == 0
            assert len(pool.released) == 1

    @pytest.mark.asyncio
    async def test_unrecognized_pool_raises(self) -> None:
        class WeirdPool:
            pass

        mgr = ConnectionBudgetManager()
        async with mgr.operation() as op:
            with pytest.raises(TypeError, match="Unsupported pool shape"):
                async with op.acquire(WeirdPool()):
                    pass


# ─── Budget enforcement (the actual point) ────────────────────────────


class TestBudgetEnforcement:
    @pytest.mark.asyncio
    async def test_budget_caps_concurrent_acquires(self) -> None:
        """Three parallel acquires through a budget=2 op should serialize 2 + 1."""
        pool = FakeAsyncpgPool()
        mgr = ConnectionBudgetManager()
        observed_active: list[int] = []
        sentinel = asyncio.Event()

        snapshot: dict[str, int] = {}
        async with mgr.operation(max_connections=2) as op:

            async def grab() -> None:
                async with op.acquire(pool):
                    observed_active.append(op.budget.active_count)
                    await sentinel.wait()

            t1 = asyncio.create_task(grab())
            t2 = asyncio.create_task(grab())
            t3 = asyncio.create_task(grab())
            # Let scheduler dispatch — 2 should grab, 3rd blocks
            await asyncio.sleep(0.01)
            assert op.budget.active_count == 2
            assert len(pool.acquired) == 2  # 3rd blocked on semaphore

            # Release — third can now grab
            sentinel.set()
            await asyncio.gather(t1, t2, t3)
            # Capture before context-exit unregisters the budget
            snapshot["peak"] = op.budget.peak_count
            snapshot["total"] = op.budget.total_acquired
            snapshot["active_at_end"] = op.budget.active_count

        assert snapshot["peak"] == 2
        assert snapshot["total"] == 3
        assert snapshot["active_at_end"] == 0

    @pytest.mark.asyncio
    async def test_independent_ops_dont_share_budget(self) -> None:
        """Two separate ops each with budget=1 can both acquire concurrently."""
        pool = FakeAsyncpgPool()
        mgr = ConnectionBudgetManager()
        sentinel = asyncio.Event()

        async def run_op() -> int:
            async with mgr.operation(max_connections=1) as op:
                async with op.acquire(pool):
                    await sentinel.wait()
                    return op.budget.active_count

        t1 = asyncio.create_task(run_op())
        t2 = asyncio.create_task(run_op())
        await asyncio.sleep(0.01)
        # Both should have acquired (different ops)
        assert len(pool.acquired) == 2
        sentinel.set()
        active_counts = await asyncio.gather(t1, t2)
        assert active_counts == [1, 1]


# ─── default singleton ────────────────────────────────────────────────


class TestDefaultManager:
    def test_default_is_lazy(self) -> None:
        # Reset to ensure deterministic test
        set_default_budget_manager(ConnectionBudgetManager())
        mgr = get_default_budget_manager()
        assert isinstance(mgr, ConnectionBudgetManager)

    def test_set_default_persists(self) -> None:
        custom = ConnectionBudgetManager(default_budget=8)
        set_default_budget_manager(custom)
        assert get_default_budget_manager() is custom
        assert get_default_budget_manager().default_budget == 8
        # restore
        set_default_budget_manager(ConnectionBudgetManager())
