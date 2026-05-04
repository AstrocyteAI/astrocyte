-- Source documents and chunks normalisation (M10).
--
-- Background:
--   Pre-M10, retained memories were anonymous flat rows in
--   astrocyte_vectors with no record of where they came from. M10 adds
--   the optional three-layer hierarchy SourceDocument → SourceChunk →
--   VectorItem so memory can preserve provenance back to source.
--
-- See:
--   - astrocyte.provider.SourceStore (the SPI)
--   - astrocyte_postgres.PostgresSourceStore (this implementation)
--   - docs/_plugins/source-documents.md (operator guide)

-- ---------------------------------------------------------------------------
-- Top-level source document
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS astrocyte_source_documents (
    id            TEXT       NOT NULL,
    bank_id       TEXT       NOT NULL,
    title         TEXT,
    source_uri    TEXT,
    content_hash  TEXT,                              -- SHA-256 hex; nullable
    content_type  TEXT,
    metadata      JSONB      NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at    TIMESTAMPTZ,
    PRIMARY KEY (bank_id, id)
);

-- Recency listing — surfaces the most-recently-stored documents first.
CREATE INDEX IF NOT EXISTS astrocyte_source_documents_bank_created_idx
    ON astrocyte_source_documents (bank_id, created_at DESC)
    WHERE deleted_at IS NULL;

-- Dedup probe: caller computes a SHA-256 of the original document content
-- and looks it up here BEFORE re-ingesting. Partial unique index so dups
-- can't slip in even if the application layer race-conditions.
CREATE UNIQUE INDEX IF NOT EXISTS astrocyte_source_documents_bank_hash_unique
    ON astrocyte_source_documents (bank_id, content_hash)
    WHERE content_hash IS NOT NULL AND deleted_at IS NULL;

-- ---------------------------------------------------------------------------
-- Chunks of a source document
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS astrocyte_source_chunks (
    id            TEXT       NOT NULL,
    bank_id       TEXT       NOT NULL,
    document_id   TEXT       NOT NULL,
    chunk_index   INTEGER    NOT NULL,
    text          TEXT       NOT NULL,
    content_hash  TEXT,                              -- SHA-256 hex of the chunk text
    metadata      JSONB      NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bank_id, id),
    FOREIGN KEY (bank_id, document_id)
        REFERENCES astrocyte_source_documents(bank_id, id) ON DELETE CASCADE,
    UNIQUE (bank_id, document_id, chunk_index)
);

-- Hot-path: load all chunks of a document in order.
CREATE INDEX IF NOT EXISTS astrocyte_source_chunks_doc_order_idx
    ON astrocyte_source_chunks (bank_id, document_id, chunk_index);

-- Dedup: caller looks up by content_hash before storing. Partial unique
-- index lets the same chunk text appear within different documents
-- without violating the constraint (we only enforce uniqueness on the
-- (bank_id, content_hash) pair when one is set).
CREATE UNIQUE INDEX IF NOT EXISTS astrocyte_source_chunks_bank_hash_unique
    ON astrocyte_source_chunks (bank_id, content_hash)
    WHERE content_hash IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Backreference column on astrocyte_vectors
-- ---------------------------------------------------------------------------
-- Nullable: backward-compatible with existing memories that have no source
-- chunk. New memories that come through the source-aware retain path get
-- their chunk_id populated; legacy memories continue to work with NULL.
ALTER TABLE astrocyte_vectors
    ADD COLUMN IF NOT EXISTS chunk_id TEXT;

-- Reverse-lookup: "what memories does this chunk produce?" — needed for
-- re-extraction flows.
CREATE INDEX IF NOT EXISTS astrocyte_vectors_chunk_idx
    ON astrocyte_vectors (chunk_id)
    WHERE chunk_id IS NOT NULL;
