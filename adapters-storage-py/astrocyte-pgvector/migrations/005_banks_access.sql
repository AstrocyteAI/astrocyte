-- Bank and access-control metadata for the reference Postgres stack.
CREATE TABLE IF NOT EXISTS astrocyte_banks (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    display_name TEXT,
    description TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS astrocyte_banks_tenant_idx
    ON astrocyte_banks (tenant_id)
    WHERE tenant_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS astrocyte_bank_access_grants (
    id BIGSERIAL PRIMARY KEY,
    bank_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    permissions TEXT[] NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    UNIQUE (bank_id, principal)
);

CREATE INDEX IF NOT EXISTS astrocyte_bank_access_grants_principal_idx
    ON astrocyte_bank_access_grants (principal)
    WHERE revoked_at IS NULL;
