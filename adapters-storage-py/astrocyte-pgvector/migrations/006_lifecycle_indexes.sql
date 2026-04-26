-- M9 lifecycle/time-travel support for existing astrocyte_vectors deployments.
ALTER TABLE astrocyte_vectors
    ADD COLUMN IF NOT EXISTS retained_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE astrocyte_vectors
    ADD COLUMN IF NOT EXISTS forgotten_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS astrocyte_vectors_bank_retained_idx
    ON astrocyte_vectors (bank_id, retained_at DESC);

CREATE INDEX IF NOT EXISTS astrocyte_vectors_bank_occurred_idx
    ON astrocyte_vectors (bank_id, occurred_at DESC)
    WHERE occurred_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS astrocyte_vectors_bank_current_idx
    ON astrocyte_vectors (bank_id)
    WHERE forgotten_at IS NULL;
