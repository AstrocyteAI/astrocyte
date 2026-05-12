-- M14.1: extend the PageIndexFact.fact_type CHECK constraint to allow
-- ``assistant_statement`` — a new grain capturing things the assistant
-- said (recommendations, answers, facts shared) with original phrasing
-- preserved. Targets the single-session-assistant LME category, which
-- M13.3 surfaced as the biggest gap when downgrading from gpt-4o to
-- gpt-4o-mini answerer (60% vs 76.7% at gpt-4o).
--
-- Idempotent: drops the old constraint if present, adds the new one if
-- absent. Safe to re-run.

SET search_path TO public;

ALTER TABLE astrocyte_pi_facts
    DROP CONSTRAINT IF EXISTS astrocyte_pi_facts_fact_type_check;

ALTER TABLE astrocyte_pi_facts
    ADD CONSTRAINT astrocyte_pi_facts_fact_type_check
    CHECK (fact_type IN (
        'experience',
        'preference',
        'world',
        'plan',
        'opinion',
        'assistant_statement'
    ));
