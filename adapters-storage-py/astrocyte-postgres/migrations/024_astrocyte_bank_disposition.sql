-- Sprint 2 #15: bank disposition + background (Hindsight parity)
--
-- Adds two columns to astrocyte_banks for per-bank prompt-shaping
-- configuration:
--
--   disposition JSONB — three traits on 1-5 scales:
--     {"skepticism": 3, "literalism": 3, "empathy": 3}
--   background TEXT — free-form context about who/what this bank serves
--                     (injected into reflect/answer prompts)
--
-- Per Hindsight's design: disposition affects REFLECT operations only
-- (answer-generation prompts), NOT recall. Recall is purely about
-- retrieving evidence; disposition shapes how that evidence is woven
-- into an answer.
--
-- Defaults are "balanced" (3 on every trait) and empty background, so
-- existing banks behave identically to pre-migration behavior. Banks
-- can be updated post-creation to customize.

ALTER TABLE astrocyte_banks
    ADD COLUMN IF NOT EXISTS disposition JSONB NOT NULL DEFAULT '{"skepticism": 3, "literalism": 3, "empathy": 3}'::jsonb;

ALTER TABLE astrocyte_banks
    ADD COLUMN IF NOT EXISTS background TEXT NOT NULL DEFAULT '';

-- Validation constraint: disposition must be a JSON object with the
-- three trait keys, each an integer 1-5. The check is permissive
-- (allows extra keys for forward compatibility) but enforces the
-- core shape so we can rely on it downstream without re-validating.
ALTER TABLE astrocyte_banks
    DROP CONSTRAINT IF EXISTS astrocyte_banks_disposition_shape_chk;

ALTER TABLE astrocyte_banks
    ADD CONSTRAINT astrocyte_banks_disposition_shape_chk CHECK (
        jsonb_typeof(disposition) = 'object'
        AND (disposition->>'skepticism')::int BETWEEN 1 AND 5
        AND (disposition->>'literalism')::int BETWEEN 1 AND 5
        AND (disposition->>'empathy')::int BETWEEN 1 AND 5
    );
