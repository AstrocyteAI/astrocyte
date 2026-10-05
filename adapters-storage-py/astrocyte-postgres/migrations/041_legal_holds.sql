-- Team memory G4: persisted legal holds.
--
-- A hold blocks forget and lifecycle deletion on a bank until it is lifted
-- (a right-to-erasure forget is not blocked; see Astrocyte.place_legal_hold).
-- Holds lived in each process's memory, so a restart dropped them and a
-- second gateway replica never saw them. ``Astrocyte.place_legal_hold`` /
-- ``lift_legal_hold`` (``/v1/admin/banks/{bank_id}/hold``) now write them
-- here, and forget / run_lifecycle re-read the bank's holds before checking.
--
-- New table. Mirrored by PostgresStore._ensure_schema (bootstrap_schema=True).
-- Idempotent.

CREATE TABLE IF NOT EXISTS astrocyte_legal_holds (
    bank_id TEXT NOT NULL,
    hold_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    set_by TEXT NOT NULL,
    set_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (bank_id, hold_id)
);
