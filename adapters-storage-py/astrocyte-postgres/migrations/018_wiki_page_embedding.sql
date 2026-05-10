-- M10.1: section-aware consolidation needs to retrieve wiki pages by
-- semantic similarity to a question. Mirrors migration 017's section
-- embedding column.
--
-- Why on wiki_pages directly (not on revisions): the search hits the
-- "current" page, not historical revisions. Pin the embedding to the
-- live row so the Postgres index plan stays simple (ANN scan + filter).
--
-- See:
--   - docs/_design/recall.md §12 (PR2.6 close-out, M10 plan)
--   - migration 007 (created astrocyte_wiki_pages without an embedding)
--   - migration 017 (mirror: section embeddings)

ALTER TABLE astrocyte_wiki_pages
    ADD COLUMN IF NOT EXISTS current_embedding vector(1536);

-- DiskANN index for the M10.1 wiki recall strategy's `<=>` queries.
-- Same backend as migration 017 (pgvectorscale, default in
-- docker/astrocyte-postgres/Dockerfile). Operators on plain pgvector
-- can substitute an HNSW index by hand.
CREATE INDEX IF NOT EXISTS ix_wiki_pages_embed
    ON astrocyte_wiki_pages
    USING diskann (current_embedding vector_cosine_ops);
