-- M28 Workstream A: per-fact "mentioned_at" timestamp on astrocyte_pi_facts.
--
-- Mirrors :attr:`astrocyte.types.MemoryFact.mentioned_at` (M27).
-- When the fact was MENTIONED in conversation, distinct from
-- ``occurred_start`` (when the event itself happened). For section-
-- anchored facts the extractor sets this to the section's
-- ``session_date``. ``NULL`` for top-level facts or legacy rows.
-- Mirrors Hindsight's ``memory_units.mentioned_at``; the answerer
-- prompt explicitly distinguishes "when it happened" from "when it
-- was discussed".
--
-- No CHECK constraint — any TIMESTAMPTZ is valid.
--
-- Idempotent: ALTER ... ADD COLUMN IF NOT EXISTS. Safe to re-run.

SET search_path TO public;

ALTER TABLE astrocyte_pi_facts
    ADD COLUMN IF NOT EXISTS mentioned_at TIMESTAMPTZ;
