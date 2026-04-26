-- Canonical SQL truth for entity/link projections plus normalized temporal facts.
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

CREATE TABLE IF NOT EXISTS astrocyte_entity_aliases (
    bank_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    evidence TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bank_id, entity_id, alias),
    FOREIGN KEY (bank_id, entity_id)
        REFERENCES astrocyte_entities (bank_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS astrocyte_entity_aliases_lookup_idx
    ON astrocyte_entity_aliases (bank_id, lower(alias));

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
    UNIQUE (bank_id, entity_a, entity_b, link_type),
    FOREIGN KEY (bank_id, entity_a)
        REFERENCES astrocyte_entities (bank_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (bank_id, entity_b)
        REFERENCES astrocyte_entities (bank_id, id)
        ON DELETE CASCADE
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
    PRIMARY KEY (bank_id, memory_id, entity_id),
    FOREIGN KEY (bank_id, entity_id)
        REFERENCES astrocyte_entities (bank_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS astrocyte_memory_entities_entity_idx
    ON astrocyte_memory_entities (bank_id, entity_id);

CREATE INDEX IF NOT EXISTS astrocyte_memory_entities_memory_idx
    ON astrocyte_memory_entities (bank_id, memory_id);

CREATE TABLE IF NOT EXISTS astrocyte_temporal_facts (
    id BIGSERIAL PRIMARY KEY,
    bank_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    temporal_phrase TEXT NOT NULL,
    anchor_time TIMESTAMPTZ,
    resolved_start TIMESTAMPTZ,
    resolved_end TIMESTAMPTZ,
    resolved_date DATE,
    date_granularity TEXT,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (bank_id, memory_id, temporal_phrase)
);

CREATE INDEX IF NOT EXISTS astrocyte_temporal_facts_resolved_idx
    ON astrocyte_temporal_facts (bank_id, resolved_date, date_granularity);

CREATE INDEX IF NOT EXISTS astrocyte_temporal_facts_memory_idx
    ON astrocyte_temporal_facts (bank_id, memory_id);
