-- B-tree for bank filtering; DiskANN ANN index for cosine search.
--
-- pgvectorscale's DiskANN is the only supported ANN backend (see
-- migration 001 and the postgres adapter README). The
-- :vector_using_clause psql variable is set by migrate.sh; when running
-- this migration with raw ``psql -f`` (no migrate.sh wrapper), the
-- fallback below provides the same DiskANN clause.
--
-- DiskANN trades slightly more disk for materially better
-- concurrent-insert throughput than HNSW — important for retain-heavy
-- Astrocyte workloads.
--
-- The index NAME keeps the legacy ``_hnsw`` suffix so existing tooling
-- that references the name by string keeps working. The name is a
-- label, not a guarantee of the underlying algorithm.
--
-- Requires PostgreSQL 15+ for CREATE INDEX CONCURRENTLY IF NOT EXISTS.
-- Each statement auto-commits under psql (no explicit transaction),
-- which CONCURRENTLY requires.

\if :{?vector_using_clause}
\else
\set vector_using_clause 'USING diskann (embedding vector_cosine_ops) WITH (num_neighbors = 50)'
\endif

CREATE INDEX CONCURRENTLY IF NOT EXISTS astrocyte_vectors_bank_idx
    ON astrocyte_vectors (bank_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS astrocyte_vectors_embedding_hnsw
    ON astrocyte_vectors
    :vector_using_clause;
