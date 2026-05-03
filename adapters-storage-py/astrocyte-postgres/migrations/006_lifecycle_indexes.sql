-- M9 lifecycle/time-travel support for existing astrocyte_vectors deployments.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'astrocyte_vectors'
          AND column_name = 'retained_at'
    ) THEN
        ALTER TABLE astrocyte_vectors
            ADD COLUMN retained_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'astrocyte_vectors'
          AND column_name = 'forgotten_at'
    ) THEN
        ALTER TABLE astrocyte_vectors
            ADD COLUMN forgotten_at TIMESTAMPTZ;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS astrocyte_vectors_bank_retained_idx
    ON astrocyte_vectors (bank_id, retained_at DESC);

CREATE INDEX IF NOT EXISTS astrocyte_vectors_bank_occurred_idx
    ON astrocyte_vectors (bank_id, occurred_at DESC)
    WHERE occurred_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS astrocyte_vectors_bank_current_idx
    ON astrocyte_vectors (bank_id)
    WHERE forgotten_at IS NULL;
