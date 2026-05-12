-- M14.6: dedicated user-preference primitive via MentalModel sub-typing.
--
-- Adds ``kind`` column to ``astrocyte_mental_models`` so the compile
-- layer can produce distinct sub-types of mental model:
--   - 'general'    (M11.2 default — stable user profile statements)
--   - 'preference' (M14.6 — consolidated user preferences with
--                   qualifier/condition/sentiment, distilled from
--                   raw preference facts at retain time)
--
-- This matches Hindsight's pattern (mental_models v4 with subtype
-- column). We're explicitly NOT adding a top-level preferences table —
-- Hindsight's deletion of the dedicated ``opinion`` fact_type
-- (migration g2h3i4j5k6l7) suggests preferences are better handled
-- at the compiled-knowledge layer than at the extract-time schema.
--
-- Idempotent: ALTER ... ADD COLUMN IF NOT EXISTS + index IF NOT EXISTS.

SET search_path TO public;

ALTER TABLE astrocyte_mental_models
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'general';

ALTER TABLE astrocyte_mental_model_versions
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'general';

-- Index for kind-filtered listing (e.g. "all preference models in a bank").
-- Partial-indexed on non-deleted rows; matches the existing scope index style.
CREATE INDEX IF NOT EXISTS astrocyte_mental_models_bank_kind_idx
    ON astrocyte_mental_models (bank_id, kind, scope)
    WHERE deleted_at IS NULL;
