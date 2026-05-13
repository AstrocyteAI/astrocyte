-- Hindsight-inspired entity resolution: trigram similarity + embedding lookups.
--
-- This migration upgrades the entity-resolution pipeline so the LLM is only
-- consulted for genuinely ambiguous (name, candidate) pairs. The two earlier
-- tiers run entirely in PostgreSQL:
--
--   1. pg_trgm trigram similarity over lower(name) — handles surface
--      variants, typos, and partial matches without an LLM call.
--   2. cosine similarity on a per-entity name embedding — handles semantic
--      aliases ("Bob" / "Robert") that string similarity misses.
--
-- See astrocyte/pipeline/entity_resolution.py and the Hindsight paper §4.1.3.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- GIN trigram index over the lowercased name column. Powers `similarity()`
-- comparisons and the `%` operator at indexed speed even when the table
-- contains millions of entities.
CREATE INDEX IF NOT EXISTS astrocyte_entities_name_trgm_idx
    ON astrocyte_entities USING gin (lower(name) gin_trgm_ops);

-- Per-entity name embedding. Nullable so existing rows remain valid;
-- the resolver treats NULL as "embedding-tier unavailable" and falls
-- back to trigram + LLM. Dimension is parameterised by migrate.sh so
-- it always matches the configured embedding model (default 128 in
-- tests, 1536 in benchmark configs using text-embedding-3-small).
\if :{?embedding_dimensions}
\else
\set embedding_dimensions 128
\endif

ALTER TABLE astrocyte_entities
    ADD COLUMN IF NOT EXISTS embedding vector(:embedding_dimensions);

-- ANN index on the embedding column for sub-millisecond cosine lookups.
-- DiskANN via pgvectorscale (same knob as migration 003); the fallback
-- below applies when this migration is run with raw ``psql -f`` outside
-- migrate.sh.
\if :{?vector_using_clause}
\else
\set vector_using_clause 'USING diskann (embedding vector_cosine_ops) WITH (num_neighbors = 50)'
\endif

CREATE INDEX IF NOT EXISTS astrocyte_entities_embedding_idx
    ON astrocyte_entities :vector_using_clause;
