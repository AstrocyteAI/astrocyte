-- B-tree for bank filtering; ANN index for cosine search.
--
-- The ANN backend is selected at migration time via the
-- :vector_using_clause psql variable, which is set by migrate.sh from
-- the VECTOR_EXTENSION env var:
--
--   VECTOR_EXTENSION=pgvector       (default) → HNSW
--   VECTOR_EXTENSION=pgvectorscale  → DiskANN
--
-- Default is HNSW with ``m=16, ef_construction=64`` — proven defaults
-- for OpenAI text-embedding-3-small dimensionality. DiskANN
-- (pgvectorscale) trades slightly more disk for materially better
-- concurrent-insert throughput, which matters for retain-heavy
-- workloads (HNSW serializes on per-page write locks; DiskANN's graph
-- structure permits more parallelism). See README for benchmarks.
--
-- The index NAME keeps the legacy ``_hnsw`` suffix even when DiskANN is
-- the active backend, so existing tooling that references the name by
-- string keeps working. The name is a label, not a guarantee of the
-- underlying algorithm.
--
-- Requires PostgreSQL 15+ for CREATE INDEX CONCURRENTLY IF NOT EXISTS.
-- Each statement auto-commits under psql (no explicit transaction),
-- which CONCURRENTLY requires.

\if :{?vector_using_clause}
\else
\set vector_using_clause 'USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)'
\endif

CREATE INDEX CONCURRENTLY IF NOT EXISTS astrocyte_vectors_bank_idx
    ON astrocyte_vectors (bank_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS astrocyte_vectors_embedding_hnsw
    ON astrocyte_vectors
    :vector_using_clause;
