-- Tier-2 recall tables (M9): PageIndex tree + Hindsight-style section graph.
--
-- Background:
--   M1–M8 recall is RAG over atomized memory_units with optional AGE-backed
--   graph. Phase A POC (file-based PageIndex over LoCoMo) showed that section-
--   grain retrieval recovers most of the v0.13.0 baseline accuracy without
--   the AGE/SFE retain cost. M9 lifts that POC into Postgres and layers
--   Hindsight's link-expansion CTE pattern on top so multi-hop and LME
--   knowledge-update categories have first-class support.
--
-- Tables (all keyed by (document_id, line_num) at section grain):
--   - astrocyte_pi_documents       — one row per conversation/document
--   - astrocyte_pi_sections        — tree nodes; recall primitive
--   - astrocyte_pi_section_entities — Hindsight unit_entities, at section grain
--   - astrocyte_pi_section_links    — Hindsight memory_links, at section grain
--   - astrocyte_pi_wiki_provenance  — cross-layer glue: wiki_page → section
--   - astrocyte_pi_wiki_contradictions — M8 wiki + Karpathy lint output
--
-- See:
--   - docs/_design/tier-2-recall.md (master design)
--   - docs/_design/adr/adr-006-three-layer-recall-stack.md
--   - docs/_design/adr/adr-007-pageindex-tree-as-section-primitive.md

-- ---------------------------------------------------------------------------
-- Documents — one per conversation / corpus document
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS astrocyte_pi_documents (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    bank_id         TEXT         NOT NULL,
    source_id       TEXT         NOT NULL,            -- e.g. "conv-26", "lme-user-42"
    md_text         TEXT         NOT NULL,            -- canonical markdown for slicing
    reference_date  TIMESTAMPTZ,                      -- "today" anchor for synth's relative-phrase resolution
    built_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (bank_id, source_id)
);

CREATE INDEX IF NOT EXISTS ix_pi_documents_bank
    ON astrocyte_pi_documents (bank_id);

-- ---------------------------------------------------------------------------
-- Sections — tree nodes; the recall primitive (ADR-007)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS astrocyte_pi_sections (
    document_id        UUID         NOT NULL REFERENCES astrocyte_pi_documents(id) ON DELETE CASCADE,
    line_num           INT          NOT NULL,
    node_id            TEXT         NOT NULL,         -- "0001"-style PageIndex node id
    title              TEXT         NOT NULL,
    summary            TEXT,
    summary_embedding  VECTOR(1536),                  -- nullable until PR2 commit A populates it
    speaker            TEXT,                          -- 'user' | 'assistant' | NULL — LME assistant-recall
    session_date       TIMESTAMPTZ,
    parent_node        TEXT,
    depth              INT          NOT NULL,
    PRIMARY KEY (document_id, line_num)
);

-- Skeleton scan (picker fetches the full ToC for one document)
CREATE INDEX IF NOT EXISTS ix_pi_sections_skeleton
    ON astrocyte_pi_sections (document_id, depth, line_num);

-- Temporal pre-filter for date-anchored picks (NULL excluded)
CREATE INDEX IF NOT EXISTS ix_pi_sections_date
    ON astrocyte_pi_sections (document_id, session_date)
    WHERE session_date IS NOT NULL;

-- LME assistant-recall: keyword search filtered to assistant turns
CREATE INDEX IF NOT EXISTS ix_pi_sections_speaker
    ON astrocyte_pi_sections (document_id, speaker)
    WHERE speaker IS NOT NULL;

-- Note: vector index on summary_embedding is added in migration 017 (PR2 commit A)
-- after we know which ANN backend (pgvector/pgvectorscale/vchord) is enabled.

-- ---------------------------------------------------------------------------
-- Section entities — Hindsight's unit_entities pattern, at section grain
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS astrocyte_pi_section_entities (
    document_id  UUID  NOT NULL,
    line_num     INT   NOT NULL,
    entity_name  TEXT  NOT NULL,
    PRIMARY KEY (document_id, line_num, entity_name),
    FOREIGN KEY (document_id, line_num)
        REFERENCES astrocyte_pi_sections(document_id, line_num) ON DELETE CASCADE
);

-- Hindsight's CTE pattern: lookup sections by entity_name in <100ms
CREATE INDEX IF NOT EXISTS ix_pi_section_entities_name
    ON astrocyte_pi_section_entities (entity_name);

-- ---------------------------------------------------------------------------
-- Section links — Hindsight's memory_links pattern, at section grain
-- link_type discriminator handles all four edge classes:
--   'semantic_knn' — kNN graph; populated at retain (PR2 commit B)
--   'causal'       — explicit causal/effect; populated at retain (PR2 commit D)
--   'supersedes'   — A is corrected by B; LME knowledge-update (PR2 commit D)
--   'elaborates'   — B is a follow-up that adds detail to A
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS astrocyte_pi_section_links (
    from_doc    UUID         NOT NULL,
    from_line   INT          NOT NULL,
    to_doc      UUID         NOT NULL,
    to_line     INT          NOT NULL,
    link_type   TEXT         NOT NULL CHECK (link_type IN ('semantic_knn', 'causal', 'supersedes', 'elaborates')),
    weight      DOUBLE PRECISION NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (from_doc, from_line, to_doc, to_line, link_type),
    FOREIGN KEY (from_doc, from_line)
        REFERENCES astrocyte_pi_sections(document_id, line_num) ON DELETE CASCADE,
    FOREIGN KEY (to_doc, to_line)
        REFERENCES astrocyte_pi_sections(document_id, line_num) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_pi_section_links_from
    ON astrocyte_pi_section_links (from_doc, from_line, link_type);

CREATE INDEX IF NOT EXISTS ix_pi_section_links_to
    ON astrocyte_pi_section_links (to_doc, to_line, link_type);

-- ---------------------------------------------------------------------------
-- Cross-layer glue — wiki page ↔ section provenance
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS astrocyte_pi_wiki_provenance (
    wiki_page_id UUID  NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
    document_id  UUID  NOT NULL,
    line_num     INT   NOT NULL,
    PRIMARY KEY (wiki_page_id, document_id, line_num),
    FOREIGN KEY (document_id, line_num)
        REFERENCES astrocyte_pi_sections(document_id, line_num) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_pi_wiki_provenance_section
    ON astrocyte_pi_wiki_provenance (document_id, line_num);

-- ---------------------------------------------------------------------------
-- Wiki contradictions — M8 + Karpathy lint output
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS astrocyte_pi_wiki_contradictions (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    page_a       UUID         NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
    page_b       UUID         NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
    reason       TEXT         NOT NULL,           -- LLM-extracted explanation
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at  TIMESTAMPTZ,
    resolution   UUID                             -- winning revision_id (FK added when revisions table is in place)
);

-- Outstanding contradictions surface fast for the lint cron
CREATE INDEX IF NOT EXISTS ix_pi_wiki_contradictions_unresolved
    ON astrocyte_pi_wiki_contradictions (page_a, page_b)
    WHERE resolved_at IS NULL;
