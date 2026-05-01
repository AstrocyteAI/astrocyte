-- Hindsight-inspired entity resolution: trigram similarity + embedding lookups.
-- Mirrors astrocyte-pgvector/migrations/009 — the AGE and pgvector adapters
-- both create astrocyte_entities idempotently, so whichever runs first
-- creates the table and the other no-ops. Either way, this migration adds
-- the trigram and embedding columns/indexes.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS astrocyte_entities_name_trgm_idx
    ON astrocyte_entities USING gin (lower(name) gin_trgm_ops);

\if :{?embedding_dimensions}
\else
\set embedding_dimensions 128
\endif

ALTER TABLE astrocyte_entities
    ADD COLUMN IF NOT EXISTS embedding vector(:embedding_dimensions);

CREATE INDEX IF NOT EXISTS astrocyte_entities_embedding_idx
    ON astrocyte_entities USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
