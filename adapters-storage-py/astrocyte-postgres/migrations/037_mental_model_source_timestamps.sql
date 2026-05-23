-- M40: per-source-id evidence timestamps on astrocyte_mental_models.
--
-- Adds the ``source_timestamps`` column — a positionally-aligned array
-- of TIMESTAMPTZ values, one per entry in the existing ``source_ids``
-- text[] column. Captures *when* each contributing memory was observed
-- in the conversation's reference timeline (not wall-clock-now), so
-- ``astrocyte.pipeline.trend.compute_trend`` can classify the model
-- as STABLE / STRENGTHENING / WEAKENING / NEW / STALE relative to a
-- query's reference date.
--
-- Mirrors the ``_obs_source_timestamps`` JSON-string column already on
-- observations (``astrocyte.pipeline.observation`` module). The MM
-- equivalent uses a real ``TIMESTAMPTZ[]`` column instead of a JSON
-- string because (a) it's a first-class table, and (b) Postgres array
-- ops give us a path to GIN-indexed range queries if "find all MMs
-- with evidence in window X" ever becomes a real access pattern.
--
-- Legacy rows whose ``source_timestamps`` is NULL continue to work —
-- ``compute_observation_trend`` (and the bench client's M40 boost) fall
-- back to ``refreshed_at`` as a single-point timestamp. Coarse, but
-- never garbage. New writes through ``MentalModelService.create`` /
-- ``MentalModelStore.upsert`` populate this column.
--
-- Same column on the versions table for parity (so revision history
-- can also be trend-computed if a future use case needs it).
--
-- Idempotent: ALTER ... ADD COLUMN IF NOT EXISTS.

SET search_path TO public;

ALTER TABLE astrocyte_mental_models
    ADD COLUMN IF NOT EXISTS source_timestamps TIMESTAMPTZ[];

ALTER TABLE astrocyte_mental_model_versions
    ADD COLUMN IF NOT EXISTS source_timestamps TIMESTAMPTZ[];

-- No index. Trend computation reads the array as part of the existing
-- per-MM SELECT path (load-by-(bank_id,model_id), list-by-bank_id) —
-- no separate index needed. Add a GIN expression index later if a
-- "MMs with evidence between dates X and Y" predicate becomes hot.
