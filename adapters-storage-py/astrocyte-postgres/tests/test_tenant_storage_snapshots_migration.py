"""Integration test for migration 038: astrocyte_tenant_storage_snapshots.

Validates the cross-tenant snapshot table that Cerebro's billing layer reads via
the gateway's ``GET /v1/admin/tenants/{tenant_id}/storage`` endpoint
(see ``docs/_design/storage-billing-endpoint.md`` §4.1).

This is the canonical schema-shape test: any drift between the table the
endpoint reads and the table the snapshot worker writes will fail this test.

The migration is applied via raw psql in production deploys; here we apply it
in-process by reading the SQL file and EXECUTE-ing it against a real Postgres,
so the test fails if the SQL file itself is malformed.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1] / "migrations"
)
MIGRATION_FILE = MIGRATIONS_DIR / "038_tenant_storage_snapshots.sql"
TABLE_NAME = "astrocyte_tenant_storage_snapshots"


@pytest.fixture
def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — skipping migration integration test")
    return url


@pytest.fixture
async def migrated_db(dsn: str):
    """Apply migration 038 to a clean slate.

    The table is cross-tenant (lives in ``public`` by design — see
    storage-billing-endpoint.md §4.1) so we just drop and recreate it
    around each test.
    """
    assert MIGRATION_FILE.exists(), f"migration file missing: {MIGRATION_FILE}"
    sql_text = MIGRATION_FILE.read_text(encoding="utf-8")

    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        await conn.execute(f"DROP TABLE IF EXISTS public.{TABLE_NAME} CASCADE")
        await conn.execute(sql_text)
        yield conn
    finally:
        await conn.execute(f"DROP TABLE IF EXISTS public.{TABLE_NAME} CASCADE")
        await conn.close()


class TestSchemaShape:
    """The table must have every column the endpoint contract (§4.1) declares."""

    @pytest.mark.asyncio
    async def test_table_exists_in_public_schema(self, migrated_db) -> None:
        # The snapshot table is cross-tenant by design — must NOT live inside
        # any tenant_<id> schema. Confirmed by querying information_schema
        # for the table name scoped to public.
        result = await (
            await migrated_db.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = %s",
                (TABLE_NAME,),
            )
        ).fetchone()
        assert result == (1,), "astrocyte_tenant_storage_snapshots must exist in public schema"

    @pytest.mark.asyncio
    async def test_columns_match_design_contract(self, migrated_db) -> None:
        # Each column in this list is consumed by the endpoint's response
        # shape — drift breaks the Cerebro contract. Types are checked so
        # that ``bytes_used`` stays bigint (not int), avoiding silent
        # overflow for tenants near the 2 GB int4 ceiling.
        rows = await (
            await migrated_db.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY ordinal_position",
                (TABLE_NAME,),
            )
        ).fetchall()
        actual = {row[0]: (row[1], row[2]) for row in rows}

        expected = {
            "tenant_id":           ("text",                        "NO"),
            "schema_name":         ("text",                        "NO"),
            "bytes_used":          ("bigint",                      "NO"),
            "heap_bytes":          ("bigint",                      "NO"),
            "index_bytes":         ("bigint",                      "NO"),
            "table_count":         ("integer",                     "NO"),
            "memory_count":        ("bigint",                      "YES"),
            "last_write_at":       ("timestamp with time zone",    "YES"),
            "measured_at":         ("timestamp with time zone",    "NO"),
            "measure_duration_ms": ("integer",                     "NO"),
            "measure_error":       ("text",                        "YES"),
        }
        assert actual == expected, (
            f"column shape drift — expected {expected}, got {actual}. "
            f"This will break the storage endpoint's response contract."
        )

    @pytest.mark.asyncio
    async def test_primary_key_is_tenant_id(self, migrated_db) -> None:
        # PK on tenant_id is the upsert target — it must be exactly tenant_id,
        # not a composite key, or the ON CONFLICT clause silently degrades to
        # INSERT-only and snapshots accumulate forever.
        rows = await (
            await migrated_db.execute(
                """
                SELECT a.attname
                  FROM pg_index i
                  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                 WHERE i.indrelid = %s::regclass
                   AND i.indisprimary
                """,
                (f"public.{TABLE_NAME}",),
            )
        ).fetchall()
        pk_columns = {row[0] for row in rows}
        assert pk_columns == {"tenant_id"}, (
            f"primary key must be (tenant_id), got {pk_columns}"
        )

    @pytest.mark.asyncio
    async def test_measured_at_index_exists(self, migrated_db) -> None:
        # Required by the bulk endpoint's ``since`` filter and by ops queries
        # like "tenants that haven't been re-measured in N hours."
        rows = await (
            await migrated_db.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = %s",
                (TABLE_NAME,),
            )
        ).fetchall()
        index_names = {row[0] for row in rows}
        # Allow either of the two indexes the design doc lists — both serve
        # the same access pattern; design §4.1 names both for completeness.
        assert any("measured_at" in name for name in index_names), (
            f"expected at least one index covering measured_at, got {index_names}"
        )


class TestUpsertBehavior:
    """The snapshot worker writes rows via UPSERT — one row per tenant, overwritten on each pass."""

    @pytest.mark.asyncio
    async def test_insert_then_upsert_overwrites_same_row(self, migrated_db) -> None:
        # Insert a tenant's first snapshot.
        tenant_id = f"t_{uuid.uuid4().hex[:12]}"
        schema_name = f"tenant_{tenant_id}"
        await migrated_db.execute(
            f"""
            INSERT INTO public.{TABLE_NAME}
              (tenant_id, schema_name, bytes_used, heap_bytes, index_bytes,
               table_count, memory_count, measured_at, measure_duration_ms)
            VALUES (%s, %s, 1000, 600, 400, 5, 42, NOW(), 12)
            """,
            (tenant_id, schema_name),
        )

        # Upsert again with new values — the row must be overwritten, not duplicated.
        await migrated_db.execute(
            f"""
            INSERT INTO public.{TABLE_NAME}
              (tenant_id, schema_name, bytes_used, heap_bytes, index_bytes,
               table_count, memory_count, measured_at, measure_duration_ms)
            VALUES (%s, %s, 2500, 1500, 1000, 6, 50, NOW(), 15)
            ON CONFLICT (tenant_id) DO UPDATE SET
              schema_name         = EXCLUDED.schema_name,
              bytes_used          = EXCLUDED.bytes_used,
              heap_bytes          = EXCLUDED.heap_bytes,
              index_bytes         = EXCLUDED.index_bytes,
              table_count         = EXCLUDED.table_count,
              memory_count        = EXCLUDED.memory_count,
              measured_at         = EXCLUDED.measured_at,
              measure_duration_ms = EXCLUDED.measure_duration_ms,
              measure_error       = EXCLUDED.measure_error
            """,
            (tenant_id, schema_name),
        )

        row = await (
            await migrated_db.execute(
                f"SELECT bytes_used, memory_count, table_count "
                f"FROM public.{TABLE_NAME} WHERE tenant_id = %s",
                (tenant_id,),
            )
        ).fetchone()
        assert row == (2500, 50, 6), (
            f"upsert must overwrite the existing row, got {row}"
        )

        # Row count for this tenant must still be exactly one — no duplication.
        count_row = await (
            await migrated_db.execute(
                f"SELECT COUNT(*) FROM public.{TABLE_NAME} WHERE tenant_id = %s",
                (tenant_id,),
            )
        ).fetchone()
        assert count_row == (1,), f"upsert duplicated rows: {count_row}"

    @pytest.mark.asyncio
    async def test_measure_error_nullable_for_successful_snapshots(self, migrated_db) -> None:
        # measure_error is populated only on failures (design §4.1) — for a
        # clean snapshot it must be allowed to be NULL.
        tenant_id = f"t_{uuid.uuid4().hex[:12]}"
        await migrated_db.execute(
            f"""
            INSERT INTO public.{TABLE_NAME}
              (tenant_id, schema_name, bytes_used, heap_bytes, index_bytes,
               table_count, measured_at, measure_duration_ms)
            VALUES (%s, %s, 0, 0, 0, 0, NOW(), 5)
            """,
            (tenant_id, f"tenant_{tenant_id}"),
        )
        row = await (
            await migrated_db.execute(
                f"SELECT measure_error FROM public.{TABLE_NAME} WHERE tenant_id = %s",
                (tenant_id,),
            )
        ).fetchone()
        assert row == (None,)

    @pytest.mark.asyncio
    async def test_measure_error_captures_failure_reason(self, migrated_db) -> None:
        # When the snapshot worker hits an error for one tenant (e.g. schema
        # dropped mid-iteration), it records the row anyway with measure_error
        # populated — so the endpoint can serve a stale-but-known value AND
        # surface the failure to ops.
        tenant_id = f"t_{uuid.uuid4().hex[:12]}"
        await migrated_db.execute(
            f"""
            INSERT INTO public.{TABLE_NAME}
              (tenant_id, schema_name, bytes_used, heap_bytes, index_bytes,
               table_count, measured_at, measure_duration_ms, measure_error)
            VALUES (%s, %s, 0, 0, 0, 0, NOW(), 0, %s)
            """,
            (tenant_id, f"tenant_{tenant_id}", "schema does not exist"),
        )
        row = await (
            await migrated_db.execute(
                f"SELECT measure_error FROM public.{TABLE_NAME} WHERE tenant_id = %s",
                (tenant_id,),
            )
        ).fetchone()
        assert row == ("schema does not exist",)
