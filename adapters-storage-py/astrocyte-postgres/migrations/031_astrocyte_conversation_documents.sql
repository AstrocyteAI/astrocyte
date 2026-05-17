-- Migration 031: conversation ↔ document many-to-many association
--
-- Lives in the composition layer: neither the Conversation Engine nor
-- the Document Engine owns this table — it is the structural link
-- between them. Both FKs cascade on delete so:
--   DELETE conversation → associations removed (documents stay)
--   DELETE document     → associations removed (conversations stay)
--
-- Enables:
--   "Which documents are attached to this conversation?"
--     SELECT document_id FROM astrocyte_conversation_documents
--     WHERE conversation_id = $1 ORDER BY attached_at;
--
--   "Which conversations reference this document?"
--     SELECT conversation_id FROM astrocyte_conversation_documents
--     WHERE document_id = $1 ORDER BY attached_at DESC;
--
-- This replaces the caller-side doc_ids tracking previously required
-- by DocumentNavigator.search() in the composition (Path 3) scenario.

-- NOTE: parent tables (astrocyte_conversations, astrocyte_documents)
-- define ``id UUID PRIMARY KEY`` — see migrations 025 + 027. The FK
-- column types here MUST be UUID to match (PG rejects text→uuid FK
-- with "incompatible types" error). Fixed 2026-05-17 during M18b
-- bench run when the type mismatch crashed bench-db-start.
CREATE TABLE IF NOT EXISTS astrocyte_conversation_documents (
    conversation_id  UUID        NOT NULL
        REFERENCES astrocyte_conversations(id) ON DELETE CASCADE,
    document_id      UUID        NOT NULL
        REFERENCES astrocyte_documents(id)     ON DELETE CASCADE,
    attached_at      timestamptz NOT NULL DEFAULT now(),
    attached_by      text,                          -- optional actor/speaker id
    PRIMARY KEY (conversation_id, document_id)
);

-- Reverse-lookup index: "which conversations reference this document?"
CREATE INDEX IF NOT EXISTS idx_conv_docs_document_id
    ON astrocyte_conversation_documents (document_id);
