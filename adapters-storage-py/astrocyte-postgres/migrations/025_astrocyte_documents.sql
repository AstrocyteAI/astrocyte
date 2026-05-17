-- M17 Phase 2: Document Engine storage — top-level documents.
--
-- Stores parsed-source metadata. Trees are stored separately in
-- astrocyte_document_nodes (migration 026); a document can exist
-- without a tree (raw source held, not yet parsed).
--
-- This table is part of the Document Engine subsystem. The Memory
-- Engine (astrocyte_pi_*, astrocyte_banks) does NOT reference it via
-- FK. If a memory row wants to point at a document or tree node, it
-- does so via opaque metadata (string in metadata JSONB), not a typed
-- foreign key. That's the composable-architecture invariant from
-- docs/_design/m17-pageindex-ingestion.md §3.

CREATE TABLE IF NOT EXISTS astrocyte_documents (
    id              UUID PRIMARY KEY,
    source_uri      TEXT NOT NULL DEFAULT '',     -- file path / URL / inline://
    content_hash    TEXT NOT NULL DEFAULT '',     -- SHA-256 hex of source bytes
    mime_type       TEXT NOT NULL DEFAULT 'text/markdown',
    title           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dedupe: same content_hash means same source bytes — useful for "skip
-- re-ingest if unchanged" flows. Not enforced as UNIQUE; the same
-- content can be ingested deliberately under different IDs.
CREATE INDEX IF NOT EXISTS astrocyte_documents_content_hash_idx
    ON astrocyte_documents (content_hash)
    WHERE content_hash <> '';

-- Recent-first listing (control plane, debugging).
CREATE INDEX IF NOT EXISTS astrocyte_documents_created_at_idx
    ON astrocyte_documents (created_at DESC);
