-- Team memory G4: erase forgotten memories for good, keeping their tombstones.
--
-- A forget on astrocyte_vectors is a soft delete (forgotten_at): the text,
-- embedding and metadata stay on disk so ``as_of`` recall can see the past.
-- An erasure (a DSAR, a team forget with ``erase: true``) must remove them,
-- yet the change feed still has to report the memory deleted, or teammates'
-- mirrors keep their copies, and the id must stay forgotten, or a re-push
-- would bring it back. ``PostgresStore.erase`` deletes the forgotten row (and
-- its temporal facts) and records here only what the feed needs: the id, its
-- bank, and the change time of the forget. ``list_changes`` and
-- ``lookup_ids`` read this table alongside astrocyte_vectors, and
-- ``insert_vectors`` skips an id found here.
--
-- New table, no rewrite of astrocyte_vectors. Mirrored by
-- PostgresStore._ensure_schema (bootstrap_schema=True). Idempotent.

CREATE TABLE IF NOT EXISTS astrocyte_vectors_tombstones (
    id TEXT PRIMARY KEY,
    bank_id TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS astrocyte_vectors_tombstones_bank_changed_idx
    ON astrocyte_vectors_tombstones (bank_id, changed_at, id COLLATE "C");
