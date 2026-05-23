-- M28 Workstream A: per-fact confidence score on astrocyte_pi_facts.
--
-- Mirrors :attr:`astrocyte.types.MemoryFact.confidence_score` (M27).
-- The extraction LLM emits a 0.0-1.0 confidence per fact; the answerer
-- uses it to hedge / abstain on low-confidence facts and trust high-
-- confidence ones. Mirrors Hindsight's ``memory_units.confidence_score``
-- (CHECK 0..1) — see
-- ``hindsight-api-slim/hindsight_api/alembic/versions/`` for parity.
--
-- NULL is the legacy / unspecified value (the field is optional in
-- MemoryFact); the CHECK only constrains non-NULL values.
--
-- Idempotent: ALTER ... ADD COLUMN IF NOT EXISTS. Safe to re-run.

SET search_path TO public;

ALTER TABLE astrocyte_pi_facts
    ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION
        CHECK (confidence_score IS NULL OR (confidence_score >= 0.0 AND confidence_score <= 1.0));
