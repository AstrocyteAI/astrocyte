-- PR2 commit A: section embeddings for the Tier-2 semantic strategy.
--
-- Adds the ``summary_embedding`` column to astrocyte_pi_sections plus an
-- ANN index. PR1 deliberately deferred this column so the schema could
-- ship without committing to a specific embedding dimension; PR2 commits
-- to ``text-embedding-3-small`` (1536 dims) by default — the
-- ``embedding_dimensions`` value can be overridden in
-- pageindex_store_config when the embedding model differs.
--
-- Index choice: ``vchordrq`` matches the rest of the M9 stack
-- (post-pgvector-HNSW migration). pgvectorscale (vectorscale) is the
-- default ANN backend per docker/astrocyte-postgres/Dockerfile, so this
-- migration uses ``vectorscale`` to align with operational convention.
-- Operators on plain pgvector can substitute an HNSW index by hand.
--
-- See:
--   - docs/_design/tier-2-recall.md §4.2 (DDL sketch)
--   - migration 015 (created the table without this column)

ALTER TABLE astrocyte_pi_sections
    ADD COLUMN IF NOT EXISTS summary_embedding vector(1536);

-- DiskANN index for the semantic strategy's `<=>` queries. Per-bank
-- partial indexes (Hindsight pattern) come later when bank counts grow;
-- at PR2 scale a single global index is fine.
CREATE INDEX IF NOT EXISTS ix_pi_sections_embed
    ON astrocyte_pi_sections
    USING diskann (summary_embedding vector_cosine_ops);
