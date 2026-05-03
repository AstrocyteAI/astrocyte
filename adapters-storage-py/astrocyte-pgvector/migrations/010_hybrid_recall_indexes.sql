-- Fast-path filters for Hindsight-parity hybrid recall.
CREATE INDEX IF NOT EXISTS astrocyte_vectors_bank_fact_type_current_idx
    ON astrocyte_vectors (bank_id, fact_type)
    WHERE forgotten_at IS NULL;
