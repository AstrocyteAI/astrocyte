-- M17 Phase 2.5: Conversation Engine storage — ordered turns.
--
-- Each row is one turn in one conversation. ``turn_index`` is the
-- 0-indexed position within the conversation; queries that reconstruct
-- a conversation read all rows for a conversation_id ordered by
-- turn_index.
--
-- ``role`` is intentionally a TEXT column (not an enum) so adapters can
-- pass custom roles like "customer"/"agent"/"system-tool" without a
-- migration. The framework standardises on user/assistant/system/tool
-- but doesn't enforce them at the schema layer.

CREATE TABLE IF NOT EXISTS astrocyte_conversation_turns (
    id               UUID PRIMARY KEY,
    conversation_id  UUID NOT NULL REFERENCES astrocyte_conversations(id) ON DELETE CASCADE,
    turn_index       INTEGER NOT NULL,
    role             TEXT NOT NULL,
    content          TEXT NOT NULL,
    "timestamp"      TIMESTAMPTZ,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (conversation_id, turn_index)
);

-- Hot path: reconstruct a conversation by reading turns in order.
CREATE INDEX IF NOT EXISTS astrocyte_conversation_turns_conv_idx
    ON astrocyte_conversation_turns (conversation_id, turn_index);

-- Role filtering — useful for "fetch all assistant turns from this thread".
CREATE INDEX IF NOT EXISTS astrocyte_conversation_turns_role_idx
    ON astrocyte_conversation_turns (conversation_id, role);

-- Timestamp range queries on a conversation.
CREATE INDEX IF NOT EXISTS astrocyte_conversation_turns_ts_idx
    ON astrocyte_conversation_turns (conversation_id, "timestamp")
    WHERE "timestamp" IS NOT NULL;
