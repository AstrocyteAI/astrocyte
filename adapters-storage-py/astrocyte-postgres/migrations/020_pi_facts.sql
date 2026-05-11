-- M12.1: fact-grain table alongside PageIndex sections.
--
-- One row = one atomic fact extracted from a section. Sections remain
-- the picker's navigation primitive (tree of session headers); facts
-- provide precision for counting / temporal / preference queries.
--
-- Mirrors Hindsight's ``memory_units`` schema at the relevant fields:
--   - fact_text / fact_type / speaker
--   - occurred_start / occurred_end (per-event time, not session time)
--   - entities (proper nouns + key:value labels — the M10.2 vocabulary
--     ports cleanly per-fact)
--   - embedding (cosine search via diskann)
--
-- See:
--   - docs/_design/recall.md §14 (M12 plan)
--   - hindsight-api-slim/hindsight_api/alembic/versions/* for parity

CREATE TABLE IF NOT EXISTS astrocyte_pi_facts (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    bank_id        TEXT         NOT NULL,
    document_id    UUID         NOT NULL,
    line_num       INT          NOT NULL,
    fact_text      TEXT         NOT NULL,
    fact_type      TEXT         NOT NULL
        CHECK (fact_type IN ('experience', 'preference', 'world', 'plan', 'opinion')),
    speaker        TEXT,
    occurred_start TIMESTAMPTZ,
    occurred_end   TIMESTAMPTZ,
    entities       TEXT[]       NOT NULL DEFAULT '{}',
    embedding      vector(1536),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    FOREIGN KEY (document_id, line_num)
        REFERENCES astrocyte_pi_sections(document_id, line_num) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_pi_facts_doc
    ON astrocyte_pi_facts (document_id);

CREATE INDEX IF NOT EXISTS ix_pi_facts_type
    ON astrocyte_pi_facts (fact_type);

CREATE INDEX IF NOT EXISTS ix_pi_facts_entities
    ON astrocyte_pi_facts USING gin (entities);

-- Partial index — most facts are conversational and don't carry an
-- event date. The rows that DO carry one are the ones temporal recall
-- needs to scan efficiently.
CREATE INDEX IF NOT EXISTS ix_pi_facts_occurred
    ON astrocyte_pi_facts (occurred_start)
    WHERE occurred_start IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_pi_facts_embedding
    ON astrocyte_pi_facts
    USING diskann (embedding vector_cosine_ops);
