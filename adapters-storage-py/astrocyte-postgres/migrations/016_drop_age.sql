-- AGE removal (M9 / ADR-008).
--
-- Background:
--   M5 introduced Apache AGE as the graph store. Hindsight demonstrated that
--   the same multi-hop / entity-graph workloads run faster on flat tables +
--   SQL CTEs (LATERAL-capped per-entity expansion). Tier-2 (M9) ships the
--   flat-table replacement at section grain (`astrocyte_pi_section_links`,
--   `astrocyte_pi_section_entities` in 015) and at memory_unit grain (added
--   here as `astrocyte_unit_links`, `astrocyte_unit_entities` for callers
--   who still need fact-grain graph operations).
--
-- This migration:
--   1. Adds the unit-grain flat-table replacements (Tier-1 graph backend).
--   2. Drops the AGE extension.
--
-- BREAKING:
--   AGE-backed deployments must rebuild from raw memory_units before
--   running this migration. There is no migration tool. See ADR-008 for
--   rationale.
--
-- See:
--   - docs/_design/adr/adr-008-section-graph-replaces-age.md

-- ---------------------------------------------------------------------------
-- Tier-1 graph (memory_unit grain) — Hindsight pattern
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS astrocyte_unit_entities (
    bank_id      TEXT  NOT NULL,
    unit_id      UUID  NOT NULL,
    entity_name  TEXT  NOT NULL,
    PRIMARY KEY (bank_id, unit_id, entity_name)
);

CREATE INDEX IF NOT EXISTS ix_unit_entities_name
    ON astrocyte_unit_entities (bank_id, entity_name);

CREATE TABLE IF NOT EXISTS astrocyte_unit_links (
    bank_id        TEXT             NOT NULL,
    from_unit_id   UUID             NOT NULL,
    to_unit_id     UUID             NOT NULL,
    link_type      TEXT             NOT NULL CHECK (link_type IN ('semantic_knn', 'causal', 'cooccurrence')),
    weight         DOUBLE PRECISION NOT NULL,
    created_at     TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bank_id, from_unit_id, to_unit_id, link_type)
);

CREATE INDEX IF NOT EXISTS ix_unit_links_from
    ON astrocyte_unit_links (bank_id, from_unit_id, link_type);

CREATE INDEX IF NOT EXISTS ix_unit_links_to
    ON astrocyte_unit_links (bank_id, to_unit_id, link_type);

-- ---------------------------------------------------------------------------
-- Drop AGE extension and any AGE-managed schemas
-- ---------------------------------------------------------------------------
--
-- Idempotent: deployments without AGE installed skip cleanly.
-- CASCADE removes any AGE-created graph schemas (per-bank graph instances
-- created by the M5 _ensure_label / per-(bank, label) partition pattern).

DROP EXTENSION IF EXISTS age CASCADE;
