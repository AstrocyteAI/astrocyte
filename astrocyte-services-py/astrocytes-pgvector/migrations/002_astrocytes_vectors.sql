-- Vector table for PgVectorStore (default name and dimensions).
-- embedding_dimensions in vector_store_config must match vector(N) below.
-- For a different width, duplicate this file or alter the column in a new migration.
CREATE TABLE IF NOT EXISTS astrocytes_vectors (
    id TEXT PRIMARY KEY,
    bank_id TEXT NOT NULL,
    embedding vector(128) NOT NULL,
    text TEXT NOT NULL,
    metadata JSONB,
    tags TEXT[],
    fact_type TEXT,
    occurred_at TIMESTAMPTZ
);
