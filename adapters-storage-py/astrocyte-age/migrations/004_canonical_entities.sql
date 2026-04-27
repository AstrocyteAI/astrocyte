-- Canonical SQL truth for AGE traversal projections.
CREATE TABLE IF NOT EXISTS astrocyte_entities (
    id TEXT NOT NULL,
    bank_id TEXT NOT NULL,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    aliases TEXT[],
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bank_id, id)
);

CREATE INDEX IF NOT EXISTS astrocyte_entities_bank_name_idx
    ON astrocyte_entities (bank_id, lower(name));

CREATE TABLE IF NOT EXISTS astrocyte_entity_links (
    id BIGSERIAL PRIMARY KEY,
    bank_id TEXT NOT NULL,
    entity_a TEXT NOT NULL,
    entity_b TEXT NOT NULL,
    link_type TEXT NOT NULL,
    evidence TEXT,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (bank_id, entity_a, entity_b, link_type)
);

CREATE INDEX IF NOT EXISTS astrocyte_entity_links_a_idx
    ON astrocyte_entity_links (bank_id, entity_a, link_type);

CREATE INDEX IF NOT EXISTS astrocyte_entity_links_b_idx
    ON astrocyte_entity_links (bank_id, entity_b, link_type);

CREATE TABLE IF NOT EXISTS astrocyte_memory_entities (
    bank_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    evidence TEXT,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bank_id, memory_id, entity_id)
);

CREATE INDEX IF NOT EXISTS astrocyte_memory_entities_entity_idx
    ON astrocyte_memory_entities (bank_id, entity_id);

CREATE INDEX IF NOT EXISTS astrocyte_memory_entities_memory_idx
    ON astrocyte_memory_entities (bank_id, memory_id);
