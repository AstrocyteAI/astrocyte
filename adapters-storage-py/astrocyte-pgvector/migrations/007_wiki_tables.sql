-- Durable WikiStore tables. SQL is the source of truth; pgvector/AGE are projections.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS astrocyte_wiki_pages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id TEXT NOT NULL,
    bank_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('topic', 'entity', 'concept')),
    scope TEXT NOT NULL,
    current_revision_id UUID,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    tags TEXT[],
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (bank_id, page_id),
    UNIQUE (bank_id, slug)
);

CREATE TABLE IF NOT EXISTS astrocyte_wiki_revisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_uuid UUID NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    markdown TEXT NOT NULL,
    summary TEXT,
    compiled_by TEXT,
    source_count INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (page_uuid, revision_number)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'astrocyte_wiki_pages_current_revision_fk'
    ) THEN
        ALTER TABLE astrocyte_wiki_pages
            ADD CONSTRAINT astrocyte_wiki_pages_current_revision_fk
            FOREIGN KEY (current_revision_id)
            REFERENCES astrocyte_wiki_revisions(id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS astrocyte_wiki_revision_sources (
    revision_id UUID NOT NULL REFERENCES astrocyte_wiki_revisions(id) ON DELETE CASCADE,
    memory_id TEXT NOT NULL,
    bank_id TEXT NOT NULL,
    quote TEXT,
    relevance DOUBLE PRECISION,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (revision_id, memory_id)
);

CREATE TABLE IF NOT EXISTS astrocyte_wiki_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_page_id UUID NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
    to_page_id UUID REFERENCES astrocyte_wiki_pages(id) ON DELETE SET NULL,
    target_slug TEXT NOT NULL,
    link_type TEXT NOT NULL DEFAULT 'related',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (from_page_id, target_slug, link_type)
);

CREATE TABLE IF NOT EXISTS astrocyte_wiki_lint_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id UUID NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
    revision_id UUID REFERENCES astrocyte_wiki_revisions(id) ON DELETE SET NULL,
    issue_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    message TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS astrocyte_wiki_pages_bank_kind_idx
    ON astrocyte_wiki_pages (bank_id, kind)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS astrocyte_wiki_pages_bank_scope_idx
    ON astrocyte_wiki_pages (bank_id, scope)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS astrocyte_wiki_revisions_page_created_idx
    ON astrocyte_wiki_revisions (page_uuid, created_at DESC);

CREATE INDEX IF NOT EXISTS astrocyte_wiki_sources_memory_idx
    ON astrocyte_wiki_revision_sources (bank_id, memory_id);

CREATE INDEX IF NOT EXISTS astrocyte_wiki_lint_open_idx
    ON astrocyte_wiki_lint_issues (page_id, status)
    WHERE status = 'open';
