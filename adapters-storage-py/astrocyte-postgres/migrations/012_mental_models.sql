-- First-class mental-model storage (M9).
--
-- Background:
--   Mental models — durable, refreshable saved-reflect summaries — were
--   previously stored as wiki pages with ``kind='concept'`` and
--   ``metadata['_mental_model'] = TRUE``. That piggyback overloaded the
--   wiki layer's lifecycle (revisions, lint issues, cross_links) for a
--   fundamentally different concept and required undocumented
--   metadata-key conventions. This migration breaks them out into their
--   own table with a clean lifecycle and per-revision history.
--
-- See:
--   - astrocyte.provider.MentalModelStore (the SPI)
--   - astrocyte_postgres.PostgresMentalModelStore (this implementation)
--   - astrocyte.pipeline.mental_model.MentalModelService (the consumer)

CREATE TABLE IF NOT EXISTS astrocyte_mental_models (
    bank_id      TEXT       NOT NULL,
    model_id     TEXT       NOT NULL,
    title        TEXT       NOT NULL,
    content      TEXT       NOT NULL,
    scope        TEXT       NOT NULL DEFAULT 'bank',
    source_ids   TEXT[]     NOT NULL DEFAULT '{}'::text[],
    revision     INTEGER    NOT NULL DEFAULT 1,
    metadata     JSONB      NOT NULL DEFAULT '{}'::jsonb,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at   TIMESTAMPTZ,
    PRIMARY KEY (bank_id, model_id)
);

-- Hot-path filter: list models in a bank with scope filter, excluding deleted.
CREATE INDEX IF NOT EXISTS astrocyte_mental_models_bank_scope_idx
    ON astrocyte_mental_models (bank_id, scope)
    WHERE deleted_at IS NULL;

-- Recency listing — surfaces the most-recently-refreshed models first.
CREATE INDEX IF NOT EXISTS astrocyte_mental_models_bank_refreshed_idx
    ON astrocyte_mental_models (bank_id, refreshed_at DESC)
    WHERE deleted_at IS NULL;

-- Per-revision history. Snapshots the prior current-revision row before
-- each upsert; lets callers diff revisions and reconstruct the timeline of
-- how a mental model evolved (Hindsight has the equivalent in
-- mental_model_versions).
CREATE TABLE IF NOT EXISTS astrocyte_mental_model_versions (
    id         BIGSERIAL    PRIMARY KEY,
    bank_id    TEXT         NOT NULL,
    model_id   TEXT         NOT NULL,
    revision   INTEGER      NOT NULL,
    title      TEXT         NOT NULL,
    content    TEXT         NOT NULL,
    source_ids TEXT[]       NOT NULL DEFAULT '{}'::text[],
    metadata   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (bank_id, model_id, revision),
    FOREIGN KEY (bank_id, model_id)
        REFERENCES astrocyte_mental_models(bank_id, model_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS astrocyte_mental_model_versions_lookup_idx
    ON astrocyte_mental_model_versions (bank_id, model_id, revision DESC);
