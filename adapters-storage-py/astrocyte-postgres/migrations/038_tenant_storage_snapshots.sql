-- M41: cross-tenant per-tenant storage snapshot table.
--
-- Backs ``GET /v1/admin/tenants/{tenant_id}/storage`` and the bulk variant
-- exposed by the gateway. The snapshot worker (astrocyte-services-py
-- astrocyte_gateway.storage_snapshot) iterates known tenant schemas every
-- ``ASTROCYTE_STORAGE_SNAPSHOT_INTERVAL_SECONDS`` (default 3600) and
-- UPSERTs one row per tenant here. The endpoint serves point lookups from
-- the table, so request latency is decoupled from measurement cost.
--
-- Design doc: docs/_design/storage-billing-endpoint.md §4.1.
--
-- This table is CROSS-TENANT by nature — it lists every tenant the
-- snapshot worker has seen, owned by ops, queried by Cerebro's billing
-- layer. It therefore lives in ``public``, NOT inside any tenant_<id>
-- schema. The migration runner's per-tenant ``SET search_path`` does not
-- apply to this table because the file pins ``public`` explicitly below.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS,
-- so the migration is safe to re-apply. Reversal is a single
-- ``DROP TABLE public.astrocyte_tenant_storage_snapshots`` (no FKs into
-- this table from anywhere else by design — see §4.1).

SET search_path TO public;

CREATE TABLE IF NOT EXISTS astrocyte_tenant_storage_snapshots (
    -- Cerebro's tenant id (opaque string; Astrocyte never parses it).
    tenant_id           TEXT        PRIMARY KEY,

    -- The Postgres schema the snapshot worker measured (e.g. ``tenant_t_8f3a...``).
    -- Surfaced in the endpoint response for ops debugging.
    schema_name         TEXT        NOT NULL,

    -- Total bytes attributable to this tenant: heap + TOAST + every index
    -- (incl. DiskANN). Derived invariant enforced by the snapshot worker:
    -- ``bytes_used = heap_bytes + index_bytes``. We deliberately do NOT
    -- encode the invariant as a CHECK constraint — the worker writes both
    -- sides atomically inside the same UPSERT, and a future tweak to the
    -- decomposition (e.g. splitting out TOAST) would otherwise require a
    -- coordinated migration.
    bytes_used          BIGINT      NOT NULL,
    heap_bytes          BIGINT      NOT NULL,
    index_bytes         BIGINT      NOT NULL,

    -- Number of relations the worker summed for this tenant. Surfaced in
    -- the endpoint's ``breakdown`` so operators can spot "tenant suddenly
    -- has 50 tables when last week it had 17" without opening psql.
    table_count         INTEGER     NOT NULL,

    -- Optional: COUNT(*) of astrocyte_vectors for this tenant — or the
    -- planner estimate (``pg_class.reltuples``) when exact counting is
    -- too expensive. NULL for deployments without a vector table at all
    -- (graph-only banks). Design §5.3 covers the choice.
    memory_count        BIGINT,

    -- Optional: max(retained_at) across astrocyte_vectors — surfaced so
    -- operators / Cerebro dashboards can show "tenant last wrote at <X>".
    last_write_at       TIMESTAMPTZ,

    -- When the snapshot worker measured this tenant. The endpoint
    -- computes ``snapshot_age_seconds = NOW() - measured_at`` and returns
    -- it so callers can reason about staleness without HTTP cache headers.
    measured_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- How long the worker spent measuring this tenant — for ops latency
    -- distributions (``astrocyte_storage_snapshot_per_tenant_duration_seconds``).
    measure_duration_ms INTEGER     NOT NULL,

    -- Populated when the snapshot for this tenant failed (e.g. schema
    -- dropped mid-iteration). The row is still UPSERTed so the endpoint
    -- can serve the LAST-KNOWN-good ``bytes_used`` value while flagging
    -- the failure to ops via the optional ``breakdown.warnings`` field.
    measure_error       TEXT
);

-- Index on measured_at for two access patterns:
--   1. Bulk endpoint's ``?since=<iso8601>`` filter — returns only tenants
--      that have been re-measured since the caller's last poll.
--   2. Ops queries like "tenants that haven't been re-measured in N hours"
--      — surfaces tenants the worker has been failing to process.
-- Composite (measured_at, tenant_id) so the bulk endpoint's cursor
-- (tenant_id-ordered pagination within a measured_at window) is index-only.
CREATE INDEX IF NOT EXISTS astrocyte_tenant_storage_snapshots_measured_at_idx
    ON astrocyte_tenant_storage_snapshots (measured_at);

CREATE INDEX IF NOT EXISTS astrocyte_tenant_storage_snapshots_measured_at_tenant_idx
    ON astrocyte_tenant_storage_snapshots (measured_at, tenant_id);
