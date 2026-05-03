-- BM25 / full-text search column on astrocyte_vectors.
--
-- Background:
--   PgVectorStore._ensure_schema() adds a text_fts tsvector column with a
--   GIN index and a BEFORE INSERT/UPDATE trigger so search_keyword() and
--   search_hybrid_semantic_bm25() can use ts_rank without a separate
--   Elasticsearch deployment. That code only runs when bootstrap_schema=true.
--
--   All benchmark configs set bootstrap_schema=false, which previously meant
--   text_fts never existed on the bench DB and every keyword/hybrid SQL
--   query failed with `column "text_fts" does not exist`. Catastrophic in
--   intent (loses every BM25 hit), silent in practice (warning-only).
--
-- This migration makes the FTS column part of the static schema so it is
-- present regardless of bootstrap_schema. All DDL is idempotent.

-- 1. Column.
ALTER TABLE astrocyte_vectors
    ADD COLUMN IF NOT EXISTS text_fts tsvector;

-- 2. GIN index.
CREATE INDEX IF NOT EXISTS astrocyte_vectors_fts_idx
    ON astrocyte_vectors USING GIN (text_fts);

-- 3. Trigger function — keeps text_fts in sync with text on every write.
CREATE OR REPLACE FUNCTION astrocyte_vectors_fts_update()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.text_fts := to_tsvector('english', COALESCE(NEW.text, ''));
    RETURN NEW;
END;
$$;

-- 4. Trigger — DROP + CREATE so the function body change above takes effect
--    on previously-deployed tables that already had an older trigger.
DROP TRIGGER IF EXISTS astrocyte_vectors_fts_trigger ON astrocyte_vectors;
CREATE TRIGGER astrocyte_vectors_fts_trigger
    BEFORE INSERT OR UPDATE OF text ON astrocyte_vectors
    FOR EACH ROW EXECUTE FUNCTION astrocyte_vectors_fts_update();

-- 5. Backfill existing rows that have NULL text_fts.
UPDATE astrocyte_vectors
   SET text_fts = to_tsvector('english', COALESCE(text, ''))
 WHERE text_fts IS NULL;
