-- M17 Phase 2.5: Conversation Engine storage — top-level conversations.
--
-- Stores ordered chat-like sessions. Turns live in
-- astrocyte_conversation_turns (migration 028); a conversation always
-- has zero-or-more turns (the FK cascade handles cleanup).
--
-- This table is part of the Conversation Engine subsystem. The Memory
-- Engine (astrocyte_pi_*, astrocyte_banks) does NOT reference it via
-- FK — same composability invariant as the Document Engine
-- (docs/_design/m17-pageindex-ingestion.md §3.2): cross-engine
-- references are opaque metadata strings, never typed foreign keys.

CREATE TABLE IF NOT EXISTS astrocyte_conversations (
    id              UUID PRIMARY KEY,
    source_uri      TEXT NOT NULL DEFAULT '',     -- "slack://...", "openai-chat://...", "bench://lme/q-..."
    title           TEXT NOT NULL DEFAULT '',
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS astrocyte_conversations_source_idx
    ON astrocyte_conversations (source_uri)
    WHERE source_uri <> '';

CREATE INDEX IF NOT EXISTS astrocyte_conversations_created_at_idx
    ON astrocyte_conversations (created_at DESC);
