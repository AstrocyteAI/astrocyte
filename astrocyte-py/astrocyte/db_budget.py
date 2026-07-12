"""Connection-pool budget enforcement per operation.

Adopted from Hindsight's ``engine/db_budget.py``. Limits the number of
concurrent DB connections any single operation can hold, preventing one
operation (e.g., a recall that fans out 5 retrieval strategies in
parallel) from monopolizing the shared connection pool and starving
other in-flight requests.

The typical failure mode this prevents:
  - pool size = 20
  - one recall fans out 5 parallel strategy queries
  - 4 concurrent recalls = 20 connections held = pool exhausted
  - 5th concurrent recall blocks indefinitely waiting for a connection

With a per-operation budget of 4, no single recall can hold more than
4 connections regardless of fan-out — pool stays available for other
in-flight work.

Pool-agnostic. Works with both psycopg ``AsyncConnectionPool``
(``pool.connection()`` ctx-manager) and asyncpg pools (``acquire()``/
``release()``).

Public API:
    ConnectionBudgetManager(default_budget=4)
        async with mgr.operation(max_connections=2) as op:
            async with op.acquire(pool) as conn:
                ...
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


# ─── budget tracking ──────────────────────────────────────────────────


@dataclass
class OperationBudget:
    """Per-operation connection budget — one semaphore + counters."""

    operation_id: str
    max_connections: int
    semaphore: asyncio.Semaphore = field(init=False)
    active_count: int = field(default=0, init=False)
    peak_count: int = field(default=0, init=False)
    total_acquired: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.max_connections)


# ─── manager ──────────────────────────────────────────────────────────


class ConnectionBudgetManager:
    """Tracks per-operation connection budgets across the process.

    Each ``operation()`` call registers a budget keyed by ID, returns a
    ``BudgetedOperation`` context. Acquires through that context are
    semaphore-bounded; the budget is unregistered on context exit.
    """

    def __init__(self, default_budget: int = 4) -> None:
        self.default_budget = default_budget
        self._operations: dict[str, OperationBudget] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def operation(
        self,
        max_connections: int | None = None,
        operation_id: str | None = None,
    ) -> AsyncIterator[BudgetedOperation]:
        """Open a budgeted operation context.

        ``max_connections`` defaults to ``default_budget``. Auto-generates
        an op_id if not supplied.
        """
        op_id = operation_id or f"op-{uuid.uuid4().hex[:12]}"
        budget = max_connections if max_connections is not None else self.default_budget
        if budget < 1:
            raise ValueError(f"max_connections must be >= 1 (got {budget})")

        async with self._lock:
            if op_id in self._operations:
                raise ValueError(f"Operation {op_id!r} already exists")
            self._operations[op_id] = OperationBudget(op_id, budget)

        try:
            yield BudgetedOperation(self, op_id)
        finally:
            async with self._lock:
                self._operations.pop(op_id, None)

    def _get_budget(self, operation_id: str) -> OperationBudget:
        budget = self._operations.get(operation_id)
        if not budget:
            raise ValueError(f"Operation {operation_id!r} not found (already exited?)")
        return budget

    @property
    def active_operations(self) -> int:
        return len(self._operations)


# ─── budgeted operation context ───────────────────────────────────────


class BudgetedOperation:
    """One operation's view of the budget. Acquire connections through here."""

    def __init__(self, manager: ConnectionBudgetManager, operation_id: str) -> None:
        self._manager = manager
        self.operation_id = operation_id

    @property
    def budget(self) -> OperationBudget:
        # Friend-class access: BudgetedOperation is the manager's per-operation
        # view, defined alongside it in this module.
        return self._manager._get_budget(self.operation_id)  # noqa: SLF001

    @asynccontextmanager
    async def acquire(self, pool: Any) -> AsyncIterator[Any]:
        """Acquire a connection within this operation's budget.

        Pool-agnostic: works with psycopg ``AsyncConnectionPool``
        (``pool.connection()`` ctx-manager) or asyncpg pools
        (``acquire()``/``release()``).
        """
        budget = self.budget
        async with budget.semaphore:
            budget.active_count += 1
            budget.total_acquired += 1
            budget.peak_count = max(budget.peak_count, budget.active_count)
            try:
                if hasattr(pool, "connection"):
                    # psycopg AsyncConnectionPool — ctx-manager
                    async with pool.connection() as conn:
                        yield conn
                elif hasattr(pool, "acquire") and hasattr(pool, "release"):
                    # asyncpg pool — acquire/release
                    conn = await pool.acquire()
                    try:
                        yield conn
                    finally:
                        await pool.release(conn)
                else:
                    raise TypeError(
                        f"Unsupported pool shape {type(pool).__name__}; "
                        "expected .connection() or .acquire()/.release()",
                    )
            finally:
                budget.active_count -= 1


# ─── module-level default ─────────────────────────────────────────────

_default_manager: ConnectionBudgetManager | None = None


def get_default_budget_manager() -> ConnectionBudgetManager:
    """Process-wide default manager. Default budget = 4 connections / operation."""
    global _default_manager
    if _default_manager is None:
        _default_manager = ConnectionBudgetManager()
    return _default_manager


def set_default_budget_manager(manager: ConnectionBudgetManager) -> None:
    global _default_manager
    _default_manager = manager
