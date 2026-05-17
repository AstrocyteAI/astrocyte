"""Domain-level audit logging for Astrocyte operations.

Adopted from Hindsight (`hindsight_api/engine/audit.py`). Captures every
retain/recall/classify/consolidate operation with structured input,
output, and metadata to a single ``astrocyte_audit_log`` table.

Design properties:

- **Fire-and-forget.** ``log()`` schedules an async write and returns
  immediately. Audit writes never block the calling operation.
- **Failure-isolating.** A failed audit write is logged at WARNING and
  swallowed. Never propagates to the caller.
- **Late-bound pool / schema.** Uses callables for pool + schema lookup
  so audit doesn't tightly couple to a specific store's lifecycle.
- **Action allowlist.** Audit is enabled per-action; default is empty
  (no audit). Production deployments opt actions in explicitly.
- **Retention sweep (opt-in).** Background task deletes entries older
  than ``retention_days``. Disabled by default (``retention_days=-1``).

Public API:
    AuditEntry(action, transport, bank_id, request, response, metadata)
    AuditLogger(pool_getter, schema_getter, enabled, allowed_actions,
                retention_days=-1)
        .log(entry)
        .log_with_timing(entry)         # context manager
        .start_retention_sweep() / .stop_retention_sweep()

Wiring pattern:
    logger = AuditLogger(
        pool_getter=lambda: store._pool,
        schema_getter=lambda: store._current_schema_or_public(),
        enabled=True,
        allowed_actions=["retain", "recall", "classify"],
    )
    logger.log(AuditEntry(action="recall", transport="bench",
                          bank_id=bank_id, request={"query": q, "top_k": 20},
                          response={"n_results": 18}))
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


# ─── data ─────────────────────────────────────────────────────────────


@dataclass
class AuditEntry:
    """A single audit log entry.

    ``request`` and ``response`` should be normalized — omit raw text
    where possible (use lengths/counts/ids) to keep the table compact
    and avoid logging sensitive content.
    """

    action: str
    transport: str  # "bench", "harness", "system", "http", "mcp", ...
    bank_id: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── JSON helpers ─────────────────────────────────────────────────────


def _json_default(obj: Any) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return "<bytes>"
    if isinstance(obj, set):
        return list(obj)  # type: ignore[return-value]
    return str(obj)


def _safe_json(data: Any) -> str | None:
    if data is None:
        return None
    try:
        return json.dumps(data, default=_json_default)
    except Exception:  # noqa: BLE001
        logger.debug("audit: JSON serialization failed", exc_info=True)
        return None


# ─── logger ───────────────────────────────────────────────────────────


_SWEEP_INTERVAL_SECONDS = 3600  # retention sweep every hour
_DEFAULT_TABLE = "astrocyte_audit_log"


class AuditLogger:
    """Fire-and-forget audit log writer.

    Pool and schema are looked up via callables at write time, so a
    single ``AuditLogger`` can serve multiple tenants whose schema
    context changes per request.
    """

    def __init__(
        self,
        *,
        pool_getter: Callable[[], Any | None],
        schema_getter: Callable[[], str] | None = None,
        enabled: bool = False,
        allowed_actions: list[str] | None = None,
        retention_days: int = -1,
        table: str = _DEFAULT_TABLE,
    ) -> None:
        self._pool_getter = pool_getter
        self._schema_getter = schema_getter or (lambda: "public")
        self._enabled = enabled
        self._allowed: frozenset[str] | None = frozenset(allowed_actions) if allowed_actions else None
        self._retention_days = retention_days
        self._table = table
        self._sweep_task: asyncio.Task[None] | None = None

    # ── status ────────────────────────────────────────────────────────

    def is_enabled(self, action: str) -> bool:
        if not self._enabled:
            return False
        if self._allowed is not None:
            return action in self._allowed
        return True

    # ── write ─────────────────────────────────────────────────────────

    def log(self, entry: AuditEntry) -> None:
        """Schedule an audit write as a background task (fire-and-forget)."""
        if not self.is_enabled(entry.action):
            return
        if entry.ended_at is None:
            entry.ended_at = datetime.now(timezone.utc)
        try:
            asyncio.create_task(self._safe_write(entry))
        except RuntimeError:
            # No running event loop (e.g. shutdown / sync context). Skip.
            logger.debug("audit: no running event loop; skip %s", entry.action)

    @asynccontextmanager
    async def log_with_timing(self, entry: AuditEntry) -> AsyncIterator[AuditEntry]:
        """Context manager that records ``ended_at`` on exit and logs.

        Usage:
            async with audit.log_with_timing(AuditEntry(action="recall", ...)) as e:
                results = await do_recall(...)
                e.response = {"n_results": len(results)}
        """
        entry.started_at = datetime.now(timezone.utc)
        try:
            yield entry
        finally:
            entry.ended_at = datetime.now(timezone.utc)
            self.log(entry)

    async def _safe_write(self, entry: AuditEntry) -> None:
        pool = self._pool_getter()
        if pool is None:
            logger.debug("audit: pool unavailable; skip %s", entry.action)
            return
        try:
            schema = self._schema_getter()
            qualified = f"{schema}.{self._table}" if schema != "public" else self._table
            await self._do_write(pool, qualified, entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "audit: write failed action=%s bank=%s: %s",
                entry.action,
                entry.bank_id,
                exc,
            )

    async def _do_write(self, pool: Any, table: str, entry: AuditEntry) -> None:
        """Write via psycopg pool (matches astrocyte_postgres adapter pattern).

        Tolerates either ``connection()`` async-context-manager or
        ``acquire()``/``release()`` shapes by feature-detecting.
        """
        sql = f"""
            INSERT INTO {table}
                (id, action, transport, bank_id, started_at, ended_at, request, response, metadata)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
        """
        params = (
            str(uuid.uuid4()),
            entry.action,
            entry.transport,
            entry.bank_id,
            entry.started_at,
            entry.ended_at,
            _safe_json(entry.request),
            _safe_json(entry.response),
            _safe_json(entry.metadata) or "{}",
        )
        # psycopg AsyncConnectionPool
        if hasattr(pool, "connection"):
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
            return
        # asyncpg-style fallback (Hindsight uses this)
        if hasattr(pool, "acquire"):
            async with pool.acquire() as conn:
                await conn.execute(sql.replace("%s", "$1$2$3$4$5$6$7$8$9"), *params)
            return
        logger.warning("audit: unrecognized pool shape: %r", type(pool))

    # ── retention sweep ───────────────────────────────────────────────

    def start_retention_sweep(self) -> None:
        if self._retention_days <= 0 or not self._enabled:
            return
        try:
            self._sweep_task = asyncio.create_task(self._sweep_loop())
        except RuntimeError:
            logger.debug("audit: cannot start sweep (no event loop)")

    async def stop_retention_sweep(self) -> None:
        if self._sweep_task is None or self._sweep_task.done():
            return
        self._sweep_task.cancel()
        try:
            await self._sweep_task
        except asyncio.CancelledError:
            pass
        self._sweep_task = None

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await self._run_sweep()
            except Exception as exc:  # noqa: BLE001
                logger.warning("audit: sweep iteration failed: %s", exc)
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)

    async def _run_sweep(self) -> None:
        pool = self._pool_getter()
        if pool is None:
            return
        schema = self._schema_getter()
        qualified = f"{schema}.{self._table}" if schema != "public" else self._table
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        sql = f"DELETE FROM {qualified} WHERE started_at < %s"
        try:
            if hasattr(pool, "connection"):
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(sql, (cutoff,))
            elif hasattr(pool, "acquire"):
                async with pool.acquire() as conn:
                    await conn.execute(sql.replace("%s", "$1"), cutoff)
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit: sweep delete failed: %s", exc)


# ─── module-level convenience: a default no-op logger ─────────────────

_DEFAULT_LOGGER: AuditLogger | None = None


def get_default_audit_logger() -> AuditLogger:
    """Return the process-wide default audit logger.

    The default is disabled (no-op) until something replaces it via
    ``set_default_audit_logger`` (e.g. at gateway startup). This lets
    pipeline code call ``get_default_audit_logger().log(...)`` without
    branching on "audit configured or not."
    """
    global _DEFAULT_LOGGER
    if _DEFAULT_LOGGER is None:
        _DEFAULT_LOGGER = AuditLogger(
            pool_getter=lambda: None,
            enabled=False,
        )
    return _DEFAULT_LOGGER


def set_default_audit_logger(logger_instance: AuditLogger) -> None:
    global _DEFAULT_LOGGER
    _DEFAULT_LOGGER = logger_instance
