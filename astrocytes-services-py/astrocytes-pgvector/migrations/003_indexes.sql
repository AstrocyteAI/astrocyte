-- B-tree for bank filtering; HNSW for cosine ANN search (<=> with vector_cosine_ops).
-- Requires PostgreSQL 15+ for CREATE INDEX CONCURRENTLY IF NOT EXISTS.
-- Each statement auto-commits under psql (no explicit transaction), which CONCURRENTLY requires.

CREATE INDEX CONCURRENTLY IF NOT EXISTS astrocytes_vectors_bank_idx
    ON astrocytes_vectors (bank_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS astrocytes_vectors_embedding_hnsw
    ON astrocytes_vectors
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
