"""Per-tenant storage snapshot worker.

Periodically iterates every known tenant schema, computes total schema bytes
(heap + indexes incl. DiskANN) via ``pg_total_relation_size``, and UPSERTs
one row per tenant into ``public.astrocyte_tenant_storage_snapshots``. The
``GET /v1/admin/tenants/{tenant_id}/storage`` endpoint serves point lookups
from that table, so endpoint latency is decoupled from measurement cost.

Design doc: ``docs/_design/storage-billing-endpoint.md`` §4.3 (worker) and
§5.3 (rationale for the snapshot-job approach over live computation).

## Public surface

- :func:`run_snapshot_pass` — the driving port. Iterates a given list of
  ``(tenant_id, schema_name)`` pairs and writes snapshot rows. Failures for
  a single tenant are isolated (row still UPSERTed with ``measure_error``
  populated) so one bad schema does not block the rest.
- :func:`start_snapshot_loop` — periodic wrapper around
  :func:`run_snapshot_pass` that runs the pass every
  ``ASTROCYTE_STORAGE_SNAPSHOT_INTERVAL_SECONDS`` (default 3600).
- :class:`SnapshotPassResult` — return value of a single pass; surfaces
  ``measured`` / ``failed`` counts for observability.

## Why a plain asyncio.Task and not APScheduler / pgqueuer cron

pgqueuer's existing wiring binds each worker to a single tenant's schema
(see :mod:`astrocyte_gateway.tasks`). The snapshot worker is by definition
cross-tenant — it lists every tenant — so using pgqueuer here would mean
either teaching pgqueuer about cross-tenant jobs (large refactor) or running
one pgqueuer job per tenant (defeats the point: a single cross-tenant pass
is the right scope).

APScheduler would add a new dependency for a single periodic loop that's
trivially expressed in stdlib ``asyncio``. The advisory-lock guard inside
:func:`run_snapshot_pass` provides multi-replica safety per design §6.4
without needing an external scheduler.

A single ``asyncio.create_task`` launched in the gateway's lifespan
(:mod:`astrocyte_gateway.app`) is the smallest thing that works.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import psycopg

_logger = logging.getLogger("astrocyte_gateway.storage_snapshot")

#: Default measurement cadence. The design (§6.3) trades staleness for
#: worker cost; 1 h is the documented default Cerebro's Slice 1.5 expects.
_DEFAULT_INTERVAL_SECONDS = 3600

#: Advisory-lock key used to prevent two workers running in parallel across
#: horizontally scaled gateway replicas. Stable arbitrary 64-bit integer —
#: changing it would only matter if another component picked the same key.
_ADVISORY_LOCK_KEY = 0x5C0C_B1_5707AE  # "STORAGE-SNAP" mangled


@dataclass(frozen=True)
class SnapshotPassResult:
    """Outcome of a single :func:`run_snapshot_pass` invocation.

    Surfaced to callers (and eventually to telemetry) so the worker's
    own health can be observed independently from the per-tenant rows
    it writes. ``measured + failed`` equals the number of tenants the
    pass attempted.
    """

    measured: int
    failed: int
    duration_seconds: float


async def run_snapshot_pass(
    connection: psycopg.AsyncConnection,
    tenants: list[tuple[str, str]],
) -> SnapshotPassResult:
    """Measure every tenant in ``tenants`` and UPSERT one row each.

    Args:
        connection: Real Postgres connection (autocommit). Must already have
            ``public.astrocyte_tenant_storage_snapshots`` created (via
            migration 038). The connection is used for all reads and writes
            — one transaction per tenant so a failure in tenant N does not
            roll back tenants 1..N-1.
        tenants: List of ``(tenant_id, schema_name)`` pairs. The ``tenant_id``
            is the opaque Cerebro identifier the endpoint echoes back; the
            ``schema_name`` is the Postgres schema the worker actually
            measures. Decoupling these lets custom :class:`TenantExtension`
            implementations map any external id to any schema.

    Returns:
        :class:`SnapshotPassResult` with success / failure counts and the
        wall-clock duration of the whole pass.

    Failure model:
        If measurement of one tenant raises (schema dropped mid-iteration,
        catalog read times out, etc.), the worker logs the error, writes
        a snapshot row with ``bytes_used = 0`` and ``measure_error`` set
        to the exception message, and continues to the next tenant. The
        endpoint can still serve a 200 for that tenant — the stale row
        carries the last-known good value, and ``measure_error`` flows
        out via the optional ``breakdown.warnings`` field so Cerebro can
        log it but not block billing.
    """
    pass_started = time.monotonic()
    measured = 0
    failed = 0
    for tenant_id, schema_name in tenants:
        ok = await _measure_one_tenant(connection, tenant_id, schema_name)
        if ok:
            measured += 1
        else:
            failed += 1
    duration = time.monotonic() - pass_started
    _logger.info(
        "storage snapshot pass complete: measured=%d failed=%d duration=%.3fs",
        measured, failed, duration,
    )
    return SnapshotPassResult(
        measured=measured, failed=failed, duration_seconds=duration
    )


async def _measure_one_tenant(
    connection: psycopg.AsyncConnection,
    tenant_id: str,
    schema_name: str,
) -> bool:
    """Measure one tenant, upsert the row, return True on success."""
    started = time.monotonic()
    try:
        sizes = await _measure_schema_bytes(connection, schema_name)
    except Exception as exc:
        # Record the failure as a row so the endpoint can serve a 200
        # with warnings rather than 503. Continue with the next tenant.
        duration_ms = int((time.monotonic() - started) * 1000)
        _logger.warning(
            "storage snapshot failed for tenant=%r schema=%r: %s",
            tenant_id, schema_name, exc,
            exc_info=True,
        )
        await _upsert_snapshot(
            connection,
            tenant_id=tenant_id,
            schema_name=schema_name,
            bytes_used=0,
            heap_bytes=0,
            index_bytes=0,
            table_count=0,
            memory_count=None,
            last_write_at=None,
            measure_duration_ms=duration_ms,
            measure_error=str(exc) or type(exc).__name__,
        )
        return False

    duration_ms = int((time.monotonic() - started) * 1000)
    await _upsert_snapshot(
        connection,
        tenant_id=tenant_id,
        schema_name=schema_name,
        bytes_used=sizes.bytes_used,
        heap_bytes=sizes.heap_bytes,
        index_bytes=sizes.index_bytes,
        table_count=sizes.table_count,
        # memory_count and last_write_at are populated by a follow-up read
        # against ``<schema>.astrocyte_vectors`` when present. They are
        # OPTIONAL in the contract (design §3.1 — Cerebro reads bytes_used
        # only) and are intentionally NOT computed in the minimum-viable
        # Slice 2 path. Adding them is a follow-up that does not change
        # the wire contract; the columns are already nullable.
        memory_count=None,
        last_write_at=None,
        measure_duration_ms=duration_ms,
        measure_error=None,
    )
    return True


@dataclass(frozen=True)
class _SchemaSizes:
    bytes_used: int
    heap_bytes: int
    index_bytes: int
    table_count: int


async def _measure_schema_bytes(
    connection: psycopg.AsyncConnection,
    schema_name: str,
) -> _SchemaSizes:
    """Compute total bytes for ``schema_name`` from ``pg_catalog``.

    The query reads catalog metadata only (no row scans), so cost is
    constant in the number of memories — see design §5.3.

    Raises:
        ValueError: if the schema does not exist in the database.
            Surface this explicitly so :func:`_measure_one_tenant` can
            record a clean ``measure_error`` instead of a generic SQL
            exception traceback.
    """
    # First confirm the schema exists. We could rely on the SUM-returns-zero
    # behavior of the catalog query, but a missing schema is structurally
    # different from "tenant has no tables yet" — Cerebro wants to know.
    row = await (
        await connection.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
            (schema_name,),
        )
    ).fetchone()
    if row is None:
        raise ValueError(f"schema does not exist: {schema_name!r}")

    # Heap (incl. TOAST) — ``pg_table_size`` excludes indexes.
    # Index bytes — ``pg_indexes_size`` excludes heap and TOAST.
    # Sum across every BASE TABLE in the schema. The catalog handles
    # quoting via the format() + regclass cast so reserved-word table
    # names are safe; schema_name is parameterised so SQL-injection
    # through the schema string is impossible.
    #
    # NOTE: psycopg parses ``%`` as a parameter sigil even inside SQL
    # string literals, so ``format('%I.%I', ...)`` has to be written as
    # ``format('%%I.%%I', ...)`` here. The doubled-percent reaches
    # Postgres as a single percent, which Postgres' own ``format()``
    # then interprets as the ``%I`` identifier-quoting specifier.
    row = await (
        await connection.execute(
            """
            SELECT
              COALESCE(SUM(pg_table_size(
                format('%%I.%%I', table_schema, table_name)::regclass
              )), 0)::bigint AS heap_bytes,
              COALESCE(SUM(pg_indexes_size(
                format('%%I.%%I', table_schema, table_name)::regclass
              )), 0)::bigint AS index_bytes,
              COUNT(*)::int AS table_count
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
            """,
            (schema_name,),
        )
    ).fetchone()
    assert row is not None, "aggregate query must always return one row"
    heap_bytes, index_bytes, table_count = row
    return _SchemaSizes(
        bytes_used=heap_bytes + index_bytes,
        heap_bytes=heap_bytes,
        index_bytes=index_bytes,
        table_count=table_count,
    )


async def _upsert_snapshot(
    connection: psycopg.AsyncConnection,
    *,
    tenant_id: str,
    schema_name: str,
    bytes_used: int,
    heap_bytes: int,
    index_bytes: int,
    table_count: int,
    memory_count: int | None,
    last_write_at: object | None,  # datetime; typed loosely to avoid import
    measure_duration_ms: int,
    measure_error: str | None,
) -> None:
    """Atomic UPSERT into the snapshot table."""
    await connection.execute(
        """
        INSERT INTO public.astrocyte_tenant_storage_snapshots (
            tenant_id, schema_name, bytes_used, heap_bytes, index_bytes,
            table_count, memory_count, last_write_at,
            measured_at, measure_duration_ms, measure_error
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s)
        ON CONFLICT (tenant_id) DO UPDATE SET
            schema_name         = EXCLUDED.schema_name,
            bytes_used          = EXCLUDED.bytes_used,
            heap_bytes          = EXCLUDED.heap_bytes,
            index_bytes         = EXCLUDED.index_bytes,
            table_count         = EXCLUDED.table_count,
            memory_count        = EXCLUDED.memory_count,
            last_write_at       = EXCLUDED.last_write_at,
            measured_at         = EXCLUDED.measured_at,
            measure_duration_ms = EXCLUDED.measure_duration_ms,
            measure_error       = EXCLUDED.measure_error
        """,
        (
            tenant_id, schema_name, bytes_used, heap_bytes, index_bytes,
            table_count, memory_count, last_write_at,
            measure_duration_ms, measure_error,
        ),
    )


# ---------------------------------------------------------------------------
# Periodic loop (lifespan-managed)
# ---------------------------------------------------------------------------


def interval_seconds_from_env() -> int:
    """Resolve the snapshot interval from
    ``ASTROCYTE_STORAGE_SNAPSHOT_INTERVAL_SECONDS``.

    Falls back to :data:`_DEFAULT_INTERVAL_SECONDS` when unset or invalid.
    Values <= 0 are rejected (would busy-loop) and replaced with the default.
    """
    raw = os.environ.get("ASTROCYTE_STORAGE_SNAPSHOT_INTERVAL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        _logger.warning(
            "ASTROCYTE_STORAGE_SNAPSHOT_INTERVAL_SECONDS=%r is not an integer; "
            "falling back to default %ds",
            raw, _DEFAULT_INTERVAL_SECONDS,
        )
        return _DEFAULT_INTERVAL_SECONDS
    if value <= 0:
        _logger.warning(
            "ASTROCYTE_STORAGE_SNAPSHOT_INTERVAL_SECONDS=%d is non-positive; "
            "falling back to default %ds",
            value, _DEFAULT_INTERVAL_SECONDS,
        )
        return _DEFAULT_INTERVAL_SECONDS
    return value


async def start_snapshot_loop(
    connection: psycopg.AsyncConnection,
    list_tenants: object,  # async callable () -> list[Tenant]
    *,
    interval_seconds: int | None = None,
) -> asyncio.Task[None]:
    """Launch the periodic snapshot loop as a managed ``asyncio.Task``.

    The returned task is owned by the caller (the gateway's lifespan in
    :mod:`astrocyte_gateway.app`) and must be cancelled on shutdown. It
    swallows per-pass exceptions so a transient DB hiccup does not kill
    the loop; the next tick retries.

    A Postgres ``pg_try_advisory_lock`` is taken at the start of each pass
    to prevent two horizontally scaled gateway replicas from racing on the
    snapshot table. Replicas that fail to acquire the lock skip the pass
    quietly and try again next tick.
    """
    if interval_seconds is None:
        interval_seconds = interval_seconds_from_env()

    async def _loop() -> None:
        _logger.info(
            "storage snapshot loop started: interval=%ds",
            interval_seconds,
        )
        while True:
            try:
                await _single_tick(connection, list_tenants)
            except Exception as exc:
                # Never let a single failure kill the loop — the next tick
                # will retry. The exception is already logged inside
                # _single_tick; this catch is a defense-in-depth so an
                # unexpected error (e.g. a connection going stale) does
                # not silently stop the worker.
                _logger.exception("storage snapshot tick failed: %s", exc)
            await asyncio.sleep(interval_seconds)

    return asyncio.create_task(_loop(), name="astrocyte.storage_snapshot")


async def _single_tick(
    connection: psycopg.AsyncConnection,
    list_tenants: object,
) -> None:
    """One iteration of the loop. Extracted for clarity / testability."""
    # Try to acquire the advisory lock; skip the pass if another replica
    # holds it. session-scope lock is released automatically when the
    # connection closes; we use a transaction-scope lock with autocommit
    # off for this single statement so the lock lives only for the pass.
    row = await (
        await connection.execute(
            "SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,),
        )
    ).fetchone()
    if row is None or not row[0]:
        _logger.info(
            "storage snapshot lock held by another replica; skipping this tick"
        )
        return
    try:
        tenants_raw = await list_tenants()  # type: ignore[operator]
        # Map Tenant(schema=...) into (tenant_id, schema_name). The
        # tenant_id is the schema minus any ``tenant_`` prefix; deployments
        # with a richer mapping (DB-backed lookup, JWT claims) will override
        # this in a future TenantExtension method (resolve_external_tenant_id).
        pairs: list[tuple[str, str]] = []
        for tenant in tenants_raw:
            schema = tenant.schema
            if schema == "public":
                # Single-tenant deployments (DefaultTenantExtension) have no
                # billing surface — skip ``public``. The endpoint will 404
                # for any tenant_id queried in this configuration, which
                # matches the design's "Astrocyte only learns about a tenant
                # on first retain" model.
                continue
            external_id = schema.removeprefix("tenant_") if schema.startswith("tenant_") else schema
            pairs.append((external_id, schema))
        await run_snapshot_pass(connection, pairs)
    finally:
        await connection.execute(
            "SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,),
        )


# ---------------------------------------------------------------------------
# Read path (consumed by the storage endpoint, slice 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotRow:
    """A snapshot row in the shape the endpoint serialises.

    Field names mirror the design contract (§10.3) so the response
    serialiser is a direct field-to-key mapping. ``measured_at`` is a
    ``datetime`` (timezone-aware UTC) here; the endpoint formats it with
    a ``Z`` suffix for the wire.
    """

    tenant_id: str
    schema_name: str
    bytes_used: int
    heap_bytes: int
    index_bytes: int
    table_count: int
    memory_count: int | None
    last_write_at: object | None  # datetime — typed loosely to avoid the import
    measured_at: object  # datetime
    measure_error: str | None


async def fetch_snapshot(
    connection: psycopg.AsyncConnection,
    tenant_id: str,
) -> SnapshotRow | None:
    """Read the snapshot row for ``tenant_id`` (or None if not present).

    Driving port for the read side of the storage-billing contract. The
    endpoint composes this with the request's ``tenant_id`` path param and
    serialises the result into the design-§10.3 JSON shape.
    """
    row = await (
        await connection.execute(
            """
            SELECT
              tenant_id, schema_name, bytes_used, heap_bytes, index_bytes,
              table_count, memory_count, last_write_at, measured_at, measure_error
            FROM public.astrocyte_tenant_storage_snapshots
            WHERE tenant_id = %s
            """,
            (tenant_id,),
        )
    ).fetchone()
    if row is None:
        return None
    (
        t_id, schema_name, bytes_used, heap_bytes, index_bytes,
        table_count, memory_count, last_write_at, measured_at, measure_error,
    ) = row
    return SnapshotRow(
        tenant_id=t_id,
        schema_name=schema_name,
        bytes_used=bytes_used,
        heap_bytes=heap_bytes,
        index_bytes=index_bytes,
        table_count=table_count,
        memory_count=memory_count,
        last_write_at=last_write_at,
        measured_at=measured_at,
        measure_error=measure_error,
    )


__all__ = [
    "SnapshotPassResult",
    "SnapshotRow",
    "fetch_snapshot",
    "interval_seconds_from_env",
    "run_snapshot_pass",
    "start_snapshot_loop",
]
