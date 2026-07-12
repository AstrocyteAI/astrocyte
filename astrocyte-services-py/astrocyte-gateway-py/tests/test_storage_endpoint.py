"""HTTP-level tests for ``GET /v1/admin/tenants/{tenant_id}/storage`` (Slice 3).

The endpoint is the consumer-facing half of the storage-billing contract
Cerebro Slice 1.5 codes against (design doc §3.1 and §10.3). It reads the
snapshot row written by the worker (Slice 2) and returns the per-tenant
``bytes_used`` figure plus a freshness indicator.

These tests drive the FastAPI app through the public HTTP surface
(``TestClient``) — the canonical driving port. The driven port is the real
``public.astrocyte_tenant_storage_snapshots`` table backing the endpoint,
populated via real INSERTs from inside the test (mirroring what the
snapshot worker would do in production). No mocks (Mandate 4).

See ``docs/_design/storage-billing-endpoint.md`` §10 for the canonical
JSON shape Cerebro pact-tests against.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


MIGRATION_FILE = (
    Path(__file__).resolve().parents[3]
    / "adapters-storage-py/astrocyte-postgres/migrations/038_tenant_storage_snapshots.sql"
)
SNAPSHOT_TABLE = "astrocyte_tenant_storage_snapshots"


@pytest.fixture(autouse=True)
def _clear_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset env between tests so ASTROCYTE_ADMIN_TOKEN never leaks across."""
    monkeypatch.delenv("ASTROCYTE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ASTROCYTE_MAX_REQUEST_BODY_BYTES", raising=False)
    monkeypatch.delenv("ASTROCYTE_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("ASTROCYTE_RATE_LIMIT_PER_SECOND", raising=False)


@pytest.fixture
def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — skipping storage endpoint integration test")
    return url


@pytest.fixture
async def snapshot_db(dsn: str):
    """Apply migration 038 against the real DB, clean on entry and exit."""
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


def _write_config(tmp_path: Path) -> Path:
    """Minimal gateway config — in-memory stores, no real provider dependencies."""
    cfg = tmp_path / "astrocyte.yaml"
    cfg.write_text(
        """
provider_tier: storage
vector_store: in_memory
llm_provider: mock
barriers: { pii: { mode: disabled } }
escalation: { degraded_mode: error }
access_control: { enabled: false }
""",
        encoding="utf-8",
    )
    return cfg


async def _insert_snapshot_row(
    conn: psycopg.AsyncConnection,
    *,
    tenant_id: str,
    schema_name: str,
    bytes_used: int,
    heap_bytes: int,
    index_bytes: int,
    table_count: int,
    measured_at: datetime | None = None,
    memory_count: int | None = None,
    last_write_at: datetime | None = None,
    measure_error: str | None = None,
) -> None:
    """Seed one snapshot row — mirrors what the worker does in production."""
    measured_clause = "NOW()" if measured_at is None else "%s"
    params: list[object] = [
        tenant_id, schema_name, bytes_used, heap_bytes, index_bytes,
        table_count, memory_count, last_write_at,
    ]
    if measured_at is not None:
        params.append(measured_at)
    params.extend([5, measure_error])
    await conn.execute(
        f"""
        INSERT INTO public.{SNAPSHOT_TABLE} (
            tenant_id, schema_name, bytes_used, heap_bytes, index_bytes,
            table_count, memory_count, last_write_at,
            measured_at, measure_duration_ms, measure_error
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, {measured_clause}, %s, %s)
        """,
        tuple(params),
    )


class TestStorageEndpointHappyPath:
    """The endpoint returns the full design-§10.3 payload for a known tenant."""

    async def test_returns_200_with_full_design_contract_shape(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot_db
    ) -> None:
        cfg = _write_config(tmp_path)
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        monkeypatch.setenv("ASTROCYTE_ADMIN_TOKEN", "secret-admin-token")
        monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])

        tenant_id = f"t_{uuid.uuid4().hex[:10]}"
        schema_name = f"tenant_{tenant_id}"
        # Anchor measured_at to a fixed past instant so snapshot_age_seconds
        # is deterministic — the endpoint reads NOW() server-side, so we
        # only assert age >= what we know it must be.
        measured_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        await _insert_snapshot_row(
            snapshot_db,
            tenant_id=tenant_id,
            schema_name=schema_name,
            bytes_used=12_345_678_901,
            heap_bytes=3_210_000_000,
            index_bytes=9_135_678_901,
            table_count=17,
            measured_at=measured_at,
        )

        from astrocyte_gateway.app import create_app

        client = TestClient(create_app())
        resp = client.get(
            f"/v1/admin/tenants/{tenant_id}/storage",
            headers={"X-Admin-Token": "secret-admin-token"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Top-level fields from design §10.3 — the canonical pact-test shape.
        assert body["tenant_id"] == tenant_id
        assert body["schema"] == schema_name
        assert body["bytes_used"] == 12_345_678_901
        # ISO 8601 UTC with Z suffix is part of the contract (§10.6) so
        # downstream callers don't have to handle timezone variants.
        assert body["measured_at"].endswith("Z"), (
            f"measured_at must be ISO 8601 UTC with Z suffix, got {body['measured_at']!r}"
        )
        # We inserted with measured_at = 120s ago, so the endpoint's
        # NOW()-measured_at will be >= 119 (allow 1s clock slop).
        assert body["snapshot_age_seconds"] >= 119

        # Breakdown is present in the design's full single-tenant response.
        # heap/index/table_count are required and non-null; memory_count /
        # last_write_at are optional (we left them NULL above).
        breakdown = body["breakdown"]
        assert breakdown["heap_bytes"] == 3_210_000_000
        assert breakdown["index_bytes"] == 9_135_678_901
        assert breakdown["table_count"] == 17
        assert breakdown["memory_count"] is None
        assert breakdown["last_write_at"] is None


class TestStorageEndpointNotFound:
    """No snapshot row → 404 with the structured error body."""

    async def test_returns_404_when_tenant_has_no_snapshot_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot_db
    ) -> None:
        cfg = _write_config(tmp_path)
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        monkeypatch.setenv("ASTROCYTE_ADMIN_TOKEN", "secret-admin-token")
        monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])

        from astrocyte_gateway.app import create_app

        client = TestClient(create_app())
        resp = client.get(
            "/v1/admin/tenants/t_does_not_exist/storage",
            headers={"X-Admin-Token": "secret-admin-token"},
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        # The body is the structured error shape from design §10.5.
        # Cerebro's StoragePollWorker matches on the ``error`` key and
        # caches bytes_used=0 in this case (treats it as a brand-new
        # tenant that hasn't written anything yet).
        assert body == {
            "error": "tenant_not_found",
            "message": "no snapshot row yet for tenant_id",
        }


class TestStorageEndpointAuth:
    """Admin token enforcement matches every other ``/v1/admin/*`` endpoint."""

    async def test_returns_401_when_token_is_wrong(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot_db
    ) -> None:
        cfg = _write_config(tmp_path)
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        monkeypatch.setenv("ASTROCYTE_ADMIN_TOKEN", "secret-admin-token")
        monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])

        from astrocyte_gateway.app import create_app

        client = TestClient(create_app())
        resp = client.get(
            "/v1/admin/tenants/t_irrelevant/storage",
            headers={"X-Admin-Token": "wrong-token-completely-different"},
        )
        assert resp.status_code == 401, resp.text

    async def test_returns_401_when_token_header_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot_db
    ) -> None:
        cfg = _write_config(tmp_path)
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        monkeypatch.setenv("ASTROCYTE_ADMIN_TOKEN", "secret-admin-token")
        monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])

        from astrocyte_gateway.app import create_app

        client = TestClient(create_app())
        resp = client.get("/v1/admin/tenants/t_irrelevant/storage")
        assert resp.status_code == 401, resp.text


class TestStorageEndpointStalenessCalculation:
    """``snapshot_age_seconds`` is computed server-side from NOW() - measured_at."""

    async def test_snapshot_age_grows_with_measured_at_offset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot_db
    ) -> None:
        # Two tenants with very different measured_at values; the older
        # snapshot must report a strictly larger snapshot_age_seconds. This
        # is the only field whose value the gateway derives at request time
        # (everything else is a passthrough from the row), so it gets its
        # own behavior test.
        cfg = _write_config(tmp_path)
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        monkeypatch.setenv("ASTROCYTE_ADMIN_TOKEN", "secret-admin-token")
        monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])

        recent_id = f"t_recent_{uuid.uuid4().hex[:6]}"
        stale_id = f"t_stale_{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc)
        await _insert_snapshot_row(
            snapshot_db,
            tenant_id=recent_id, schema_name=f"tenant_{recent_id}",
            bytes_used=100, heap_bytes=80, index_bytes=20, table_count=1,
            measured_at=now - timedelta(seconds=30),
        )
        await _insert_snapshot_row(
            snapshot_db,
            tenant_id=stale_id, schema_name=f"tenant_{stale_id}",
            bytes_used=100, heap_bytes=80, index_bytes=20, table_count=1,
            measured_at=now - timedelta(hours=3),
        )

        from astrocyte_gateway.app import create_app

        client = TestClient(create_app())
        recent = client.get(
            f"/v1/admin/tenants/{recent_id}/storage",
            headers={"X-Admin-Token": "secret-admin-token"},
        ).json()
        stale = client.get(
            f"/v1/admin/tenants/{stale_id}/storage",
            headers={"X-Admin-Token": "secret-admin-token"},
        ).json()

        # Recent: about 30s old. Stale: about 3h = 10800s old. Allow generous
        # slop for clock skew + test execution time, but the ratio must be
        # unmistakable.
        assert recent["snapshot_age_seconds"] < 120, (
            f"recent snapshot reported as too old: {recent['snapshot_age_seconds']}"
        )
        assert stale["snapshot_age_seconds"] > 10_000, (
            f"stale snapshot reported as too fresh: {stale['snapshot_age_seconds']}"
        )
        assert stale["snapshot_age_seconds"] > recent["snapshot_age_seconds"]


class TestStorageEndpointAuthUnconfigured:
    """When ASTROCYTE_ADMIN_TOKEN is unset, the endpoint inherits the existing
    /v1/admin/* convention (auth is off — see design Q1, deferred to ops).

    This test pins the current behaviour so a future change to flip the
    endpoint to fail-closed is a deliberate, visible policy decision rather
    than a silent regression."""

    async def test_no_token_configured_means_no_auth_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot_db
    ) -> None:
        cfg = _write_config(tmp_path)
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        # Critically: do NOT set ASTROCYTE_ADMIN_TOKEN.
        monkeypatch.delenv("ASTROCYTE_ADMIN_TOKEN", raising=False)
        monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])

        tenant_id = f"t_{uuid.uuid4().hex[:10]}"
        await _insert_snapshot_row(
            snapshot_db,
            tenant_id=tenant_id, schema_name=f"tenant_{tenant_id}",
            bytes_used=42, heap_bytes=30, index_bytes=12, table_count=1,
        )

        from astrocyte_gateway.app import create_app

        client = TestClient(create_app())
        # No X-Admin-Token header at all — endpoint must still return 200
        # to match the existing convention. Design Q1 (open) suggests
        # eventually flipping this to fail-closed; this test pins the
        # current behaviour so that change is intentional.
        resp = client.get(f"/v1/admin/tenants/{tenant_id}/storage")
        assert resp.status_code == 200, resp.text
        assert resp.json()["bytes_used"] == 42
