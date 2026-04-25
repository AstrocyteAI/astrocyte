-- Memory-to-entity associations live in a regular PostgreSQL table.
-- Memory IDs originate in the VectorStore (pgvector); entity IDs originate
-- in the AGE graph.  A join table in plain SQL keeps cross-store lookups
-- cheap without requiring a graph traversal.
CREATE TABLE IF NOT EXISTS astrocyte_age_mem_entity (
    id          BIGSERIAL PRIMARY KEY,
    bank_id     TEXT        NOT NULL,
    memory_id   TEXT        NOT NULL,
    entity_id   TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (bank_id, memory_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_age_mem_entity_bank_entity
    ON astrocyte_age_mem_entity (bank_id, entity_id);

CREATE INDEX IF NOT EXISTS idx_age_mem_entity_bank_memory
    ON astrocyte_age_mem_entity (bank_id, memory_id);
