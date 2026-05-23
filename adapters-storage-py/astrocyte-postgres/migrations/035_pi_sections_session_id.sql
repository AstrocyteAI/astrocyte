-- 035_pi_sections_session_id.sql  (M31 Fix 2 — generalizable session_filter on recall)
--
-- Adds an opaque session identifier to ``astrocyte_pi_sections`` so the
-- recall path can filter by session. In real conversation systems the
-- application typically knows which session a query is about; passing
-- ``session_id`` to recall lets the SQL pre-filter to that session
-- without relying on bench-specific category labels or prompt tricks.
--
-- TEXT (not UUID) because session identity is opaque to Astrocyte —
-- callers may use timestamps (`ts:1715212800`), UUIDs, slugs, or
-- application-native IDs. We just match on equality.
--
-- NULL is the default + the back-compat shape for legacy rows ingested
-- via document paths (markdown documents have no session concept). The
-- recall filter is a no-op when ``session_filter=None`` (the default).
--
-- Idempotent (``ADD COLUMN IF NOT EXISTS``). In-process bootstrap path
-- in ``pageindex_store._ddl_sections_alter`` (M31) mirrors this so fresh
-- DBs (no migration history) end up identical to migrated DBs.

SET search_path TO public, pg_catalog;

ALTER TABLE astrocyte_pi_sections
    ADD COLUMN IF NOT EXISTS session_id TEXT NULL;

-- Partial index — only sections that actually have a session_id pay the
-- index cost. Document-ingest paths don't populate this, so they're
-- skipped by the partial-index predicate.
CREATE INDEX IF NOT EXISTS astrocyte_pi_sections_session_id_idx
    ON astrocyte_pi_sections (session_id)
    WHERE session_id IS NOT NULL;
