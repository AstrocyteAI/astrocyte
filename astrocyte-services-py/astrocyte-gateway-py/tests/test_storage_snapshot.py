"""Integration tests for the storage-snapshot worker (Slice 2).

The worker iterates known tenant schemas, computes total schema bytes via
``pg_total_relation_size``, and UPSERTs one row per tenant into
``public.astrocyte_tenant_storage_snapshots``. It is the producer for the
``GET /v1/admin/tenants/{id}/storage`` endpoint (Slice 3).

The driving port under test is the public async function
``run_snapshot_pass(connection, schemas) -> SnapshotPassResult``. Each test
enters through that port and asserts at the driven-port boundary (rows in
the snapshot table). Real Postgres only — no mocks (Mandate 4 / Mandate 6).

See ``docs/_design/storage-billing-endpoint.md`` §4.3 and §5.3.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest

# Gateway tests use anyio (not pytest-asyncio) — match the existing convention
# (see tests/test_gateway_tasks_multitenant.py).
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


MIGRATION_FILE = (
    Path(__file__).resolve().parents[3]
    / "adapters-storage-py/astrocyte-postgres/migrations/038_tenant_storage_snapshots.sql"
)
SNAPSHOT_TABLE = "astrocyte_tenant_storage_snapshots"


@pytest.fixture
def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — skipping snapshot worker integration test")
    return url


@pytest.fixture
async def db(dsn: str):
    """Apply migration 038 and yield a real Postgres connection.

    Drops the snapshot table both before and after the test so the table
    starts empty regardless of prior test state.
    """
    assert MIGRATION_FILE.exists(), f"migration file missing: {MIGRATION_FILE}"
    sql_text = MIGRATION_FILE.read_text(encoding="utf-8")

    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    await conn.execute(f"DROP TABLE IF EXISTS public.{SNAPSHOT_TABLE} CASCADE")
    await conn.execute(sql_text)
    try:
        yield conn
    finally:
        await conn.execute(f"DROP TABLE IF EXISTS public.{SNAPSHOT_TABLE} CASCADE")
        await conn.close()


async def _make_tenant_schema(conn: psycopg.AsyncConnection, schema: str) -> None:
    """Create a tenant schema with one tiny table so pg_total_relation_size > 0."""
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    # A small table with a few rows — enough that pg_total_relation_size
    # reports a non-zero figure (heap pages allocate at the page granularity,
    # so even an empty table reports the catalog overhead). Adding a few
    # rows guarantees heap_bytes > 0.
    await conn.execute(
        f'CREATE TABLE IF NOT EXISTS "{schema}".tiny (id INT PRIMARY KEY, payload TEXT)'
    )
    await conn.execute(
        f'INSERT INTO "{schema}".tiny VALUES (1, %s), (2, %s)',
        ("x" * 1000, "y" * 1000),
    )


async def _drop_tenant_schema(conn: psycopg.AsyncConnection, schema: str) -> None:
    await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
async def two_tenants(db):
    """Create two real tenant schemas and tear them down afterwards."""
    suffix = uuid.uuid4().hex[:8]
    schema_a = f"tenant_iso_a_{suffix}"
    schema_b = f"tenant_iso_b_{suffix}"
    await _make_tenant_schema(db, schema_a)
    await _make_tenant_schema(db, schema_b)
    try:
        yield db, schema_a, schema_b
    finally:
        await _drop_tenant_schema(db, schema_a)
        await _drop_tenant_schema(db, schema_b)


class TestSnapshotPassHappyPath:
    """run_snapshot_pass measures every tenant and writes a row per tenant."""

    async def test_writes_one_row_per_tenant_with_positive_bytes_used(
        self, two_tenants
    ) -> None:
        from astrocyte_gateway.storage_snapshot import run_snapshot_pass

        conn, schema_a, schema_b = two_tenants

        # Schema-name → tenant-id mapping mirrors the gateway's convention:
        # ``tenant_<id>`` schemas map back to ``<id>``. The worker accepts
        # an explicit (tenant_id, schema_name) list so the test doesn't
        # depend on the runtime TenantExtension.
        tenants = [
            (schema_a.removeprefix("tenant_"), schema_a),
            (schema_b.removeprefix("tenant_"), schema_b),
        ]
        result = await run_snapshot_pass(conn, tenants)

        # Both tenants must have been measured successfully.
        assert result.measured == 2
        assert result.failed == 0

        rows = await (
            await conn.execute(
                f"SELECT tenant_id, schema_name, bytes_used, heap_bytes, index_bytes, "
                f"table_count, measure_error "
                f"FROM public.{SNAPSHOT_TABLE} "
                f"ORDER BY tenant_id"
            )
        ).fetchall()
        assert len(rows) == 2
        for tenant_id, schema_name, bytes_used, heap, index, tcount, err in rows:
            # The tiny table we created has heap > 0 (we wrote 2 rows of ~1 KB)
            # and a PK index, so index_bytes > 0 too.
            assert bytes_used > 0, f"tenant {tenant_id} has zero bytes_used"
            assert heap > 0, f"tenant {tenant_id} has zero heap_bytes"
            assert bytes_used == heap + index, (
                f"invariant bytes_used = heap + index violated: "
                f"{bytes_used} != {heap} + {index}"
            )
            assert tcount >= 1, f"tenant {tenant_id} has table_count={tcount}"
            assert err is None, f"tenant {tenant_id} unexpectedly errored: {err}"

    async def test_second_pass_overwrites_first_pass_row(self, two_tenants) -> None:
        from astrocyte_gateway.storage_snapshot import run_snapshot_pass

        conn, schema_a, _ = two_tenants
        tenants = [(schema_a.removeprefix("tenant_"), schema_a)]

        # First pass — observe the row count.
        await run_snapshot_pass(conn, tenants)
        first_measured_at = (
            await (
                await conn.execute(
                    f"SELECT measured_at FROM public.{SNAPSHOT_TABLE} "
                    f"WHERE schema_name = %s",
                    (schema_a,),
                )
            ).fetchone()
        )[0]

        # Add data, then second pass.
        await conn.execute(
            f'INSERT INTO "{schema_a}".tiny SELECT g, %s FROM generate_series(100, 200) g',
            ("z" * 2000,),
        )
        # VACUUM/ANALYZE-equivalent: force the catalog to reflect new bytes.
        await conn.execute(f'VACUUM ANALYZE "{schema_a}".tiny')

        await run_snapshot_pass(conn, tenants)

        # Still exactly one row for this tenant (UPSERT, not INSERT).
        count_row = await (
            await conn.execute(
                f"SELECT COUNT(*) FROM public.{SNAPSHOT_TABLE} WHERE schema_name = %s",
                (schema_a,),
            )
        ).fetchone()
        assert count_row == (1,), f"second pass duplicated rows: {count_row}"

        # measured_at advanced.
        second_measured_at = (
            await (
                await conn.execute(
                    f"SELECT measured_at FROM public.{SNAPSHOT_TABLE} "
                    f"WHERE schema_name = %s",
                    (schema_a,),
                )
            ).fetchone()
        )[0]
        assert second_measured_at >= first_measured_at, (
            f"measured_at did not advance: {first_measured_at} -> {second_measured_at}"
        )


class TestSnapshotPassFailureIsolation:
    """A failure for one tenant must not block measurement of the others."""

    async def test_one_missing_schema_does_not_block_others(self, two_tenants) -> None:
        from astrocyte_gateway.storage_snapshot import run_snapshot_pass

        conn, schema_a, schema_b = two_tenants

        # Pass three tenants — two real, one whose schema doesn't exist.
        # The worker must record the missing one as failed (with
        # measure_error populated) AND still write rows for the two real ones.
        missing_schema = f"tenant_missing_{uuid.uuid4().hex[:8]}"
        tenants = [
            (schema_a.removeprefix("tenant_"), schema_a),
            (missing_schema.removeprefix("tenant_"), missing_schema),
            (schema_b.removeprefix("tenant_"), schema_b),
        ]
        result = await run_snapshot_pass(conn, tenants)

        # 2 successful, 1 failed.
        assert result.measured == 2
        assert result.failed == 1

        # The two real tenants got rows with no error.
        for schema in (schema_a, schema_b):
            row = await (
                await conn.execute(
                    f"SELECT bytes_used, measure_error FROM public.{SNAPSHOT_TABLE} "
                    f"WHERE schema_name = %s",
                    (schema,),
                )
            ).fetchone()
            assert row is not None, f"missing snapshot for {schema}"
            bytes_used, err = row
            assert bytes_used > 0
            assert err is None

        # The missing tenant got a row too — with bytes_used=0 and
        # measure_error populated, so the endpoint can serve a 200 with
        # warnings rather than a 503.
        row = await (
            await conn.execute(
                f"SELECT bytes_used, measure_error FROM public.{SNAPSHOT_TABLE} "
                f"WHERE schema_name = %s",
                (missing_schema,),
            )
        ).fetchone()
        assert row is not None, "missing-schema tenant must still get a row"
        bytes_used, err = row
        assert bytes_used == 0
        assert err is not None and len(err) > 0, (
            f"measure_error must be populated for failures, got {err!r}"
        )


class TestSnapshotPassEmptyInput:
    """An empty tenant list is a valid no-op, not an error."""

    async def test_empty_tenant_list_writes_nothing_and_does_not_error(
        self, db
    ) -> None:
        from astrocyte_gateway.storage_snapshot import run_snapshot_pass

        result = await run_snapshot_pass(db, [])
        assert result.measured == 0
        assert result.failed == 0

        count_row = await (
            await db.execute(f"SELECT COUNT(*) FROM public.{SNAPSHOT_TABLE}")
        ).fetchone()
        assert count_row == (0,)
