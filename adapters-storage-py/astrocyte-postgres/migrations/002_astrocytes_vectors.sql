-- Vector table for PgVectorStore.
-- embedding_dimensions in vector_store_config must match vector(N) below.
-- migrate.sh passes psql variable :embedding_dimensions, defaulting to 128.
\if :{?embedding_dimensions}
\else
\set embedding_dimensions 128
\endif

CREATE TABLE IF NOT EXISTS astrocyte_vectors (
    id TEXT PRIMARY KEY,
    bank_id TEXT NOT NULL,
    embedding vector(:embedding_dimensions) NOT NULL,
    text TEXT NOT NULL,
    metadata JSONB,
    tags TEXT[],
    fact_type TEXT,
    occurred_at TIMESTAMPTZ,
    retained_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    forgotten_at TIMESTAMPTZ
);
