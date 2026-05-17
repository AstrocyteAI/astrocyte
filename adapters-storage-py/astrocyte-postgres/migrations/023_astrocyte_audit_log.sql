-- Sprint 1 #9: domain-level audit logging
--
-- Hindsight ships an audit table that captures every retain/recall/reflect
-- operation with the request shape, response shape, and structured metadata.
-- We adopt the same shape because operational debugging at scale is
-- bench-orthogonal but production-critical: "why did the agent retrieve
-- this?" needs an audit trail, not just OTel traces.
--
-- The table is intentionally a single denormalized log — query patterns are
-- "last N entries for bank X" and "last N entries for action Y". Indexes
-- target those.
--
-- Fire-and-forget writes from the audit logger; failures never propagate
-- to the calling operation.

CREATE TABLE IF NOT EXISTS astrocyte_audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action          TEXT NOT NULL,
    transport       TEXT NOT NULL,           -- "bench", "harness", "system", "http", "mcp" (future)
    bank_id         TEXT,                    -- nullable for system-level actions
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    request         JSONB,                   -- normalized inputs (omits raw text where possible)
    response        JSONB,                   -- normalized outputs (counts, ids, metadata)
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Recent-first queries by bank
CREATE INDEX IF NOT EXISTS astrocyte_audit_log_bank_started_idx
    ON astrocyte_audit_log (bank_id, started_at DESC);

-- Recent-first queries by action
CREATE INDEX IF NOT EXISTS astrocyte_audit_log_action_started_idx
    ON astrocyte_audit_log (action, started_at DESC);

-- Retention sweep range scan
CREATE INDEX IF NOT EXISTS astrocyte_audit_log_started_idx
    ON astrocyte_audit_log (started_at);
