-- Team memory G3: change feed over astrocyte_vectors.
--
-- ``GET /v1/banks/{bank_id}/changes`` (``VectorStore.list_changes``) pages
-- every change to a synced row, in ``(changed_at, id)`` order, so a
-- teammate's mirror can pull what changed since its cursor
-- (docs/_design/team-memory.md §8, G3).
--
-- ``changed_at`` is the time of the row's last change to any synced field.
-- PostgresStore sets it on every write: an insert takes the row's
-- retained_at; an upsert over an existing row (overwrite, restore of a
-- forgotten id, a metadata rewrite through store_vectors) takes
-- GREATEST(retained_at, NOW()); a forget takes GREATEST(retained_at, NOW()).
-- Any future write path that changes a synced field (claim status, trust,
-- staleness flags) must bump it too, or mirrors never see the change.
--
-- Backfill without a rewrite. The column is added NULL (a catalog-only
-- change) and rows written before this migration stay NULL: the feed reads
-- ``COALESCE(changed_at, GREATEST(retained_at, forgotten_at))``, which for
-- them is exactly the backfill value max(retained_at, forgotten_at), and the
-- index below is on that expression. An UPDATE backfill would write a new
-- version of every row, each inserted into the DiskANN index; a generated
-- column would rewrite the table under an ACCESS EXCLUSIVE lock and rebuild
-- every index. This index only reads the table (writes wait while it builds;
-- reads don't). On a very large table, an operator can build it beforehand
-- with CREATE INDEX CONCURRENTLY under the same name, and this file then
-- skips it. Rows get a concrete changed_at the next time they are written.
--
-- PostgresStore.list_changes selects and orders by exactly this expression,
-- so the planner uses the index. ``id`` is indexed COLLATE "C" because the
-- feed orders and compares ids byte-wise: the same order on every database
-- locale, and the same as the SQLite store.
--
-- Mirrored by PostgresStore._ensure_schema (bootstrap_schema=True).
-- Idempotent: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

ALTER TABLE astrocyte_vectors
    ADD COLUMN IF NOT EXISTS changed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS astrocyte_vectors_bank_changed_idx
    ON astrocyte_vectors (bank_id, (COALESCE(changed_at, GREATEST(retained_at, forgotten_at))), id COLLATE "C");
