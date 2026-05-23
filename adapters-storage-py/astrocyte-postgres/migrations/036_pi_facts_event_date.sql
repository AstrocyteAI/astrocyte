-- 036_pi_facts_event_date.sql  (M31 Fix 4 — temporal-resolution at retain)
--
-- Adds ``event_date`` — a single absolute datetime resolved at retain
-- time from relative phrases in the fact's text ("last Tuesday",
-- "3 days ago") against the section's ``session_date`` as anchor.
-- See ``astrocyte.pipeline.temporal_resolution`` for the resolver
-- contract.
--
-- Distinct from existing temporal columns:
--   - ``occurred_start`` / ``occurred_end``: LLM-emitted explicit
--     event-time range for facts that span multiple days
--   - ``mentioned_at``: when the user discussed the fact (M28-B)
--   - ``event_date`` (NEW): single most-prominent date for the fact,
--     resolved deterministically from relative phrases
--
-- The answerer reads ``event_date`` instead of forcing gpt-4o-mini
-- to do date math at query time — moves the deterministic part of
-- the work out of the LLM call. This is an architectural divergence
-- from Hindsight (which keeps date math in the answerer prompt).
--
-- Idempotent (``ADD COLUMN IF NOT EXISTS``). In-process bootstrap
-- path in ``pageindex_store._ddl_facts_alter`` mirrors this.

SET search_path TO public, pg_catalog;

DO $$
BEGIN
    IF to_regclass('astrocyte_pi_facts') IS NOT NULL THEN
        ALTER TABLE astrocyte_pi_facts
            ADD COLUMN IF NOT EXISTS event_date TIMESTAMPTZ NULL;
    END IF;
END$$;
