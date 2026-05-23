-- M21: structured-doc JSONB column on astrocyte_mental_models.
--
-- Adds the typed structured-document representation alongside the
-- existing raw-markdown ``content`` column. New writes through
-- :meth:`astrocyte.pipeline.mental_model.MentalModelService.create`
-- (with ``structured_doc=...``) populate this column directly;
-- :meth:`MentalModelStore.update_via_ops` writes both ``content`` (re-
-- rendered from the structured doc) and ``structured_doc`` (the
-- updated typed representation) atomically per upsert.
--
-- Legacy rows whose ``structured_doc`` is NULL continue to work —
-- :func:`astrocyte.pipeline.structured_doc.parse_markdown` lazily
-- converts ``content`` on the first ``update_via_ops`` call (one-shot,
-- never repeated).
--
-- Why JSONB and not a separate table:
--   - The structured doc IS the model — separation would introduce
--     transactional coupling on every read.
--   - JSONB indexing (GIN on jsonb_path_ops) is fine for the access
--     patterns we have (per-model load, list-all-in-bank).
--   - Mirrors Hindsight's ``mental_models.structured_doc`` JSONB column
--     in ``hindsight-api-slim/hindsight_api/alembic/versions/``.
--
-- Idempotent: ALTER ... ADD COLUMN IF NOT EXISTS.

SET search_path TO public;

ALTER TABLE astrocyte_mental_models
    ADD COLUMN IF NOT EXISTS structured_doc JSONB;

ALTER TABLE astrocyte_mental_model_versions
    ADD COLUMN IF NOT EXISTS structured_doc JSONB;

-- No index — current access patterns (load-by-(bank_id,model_id),
-- list-by-bank_id) hit the existing PK + bank_scope index. Add a GIN
-- index on structured_doc only when section-content search lands as a
-- real use case (e.g. "find all mental models whose Status section
-- mentions 'blocked'").
