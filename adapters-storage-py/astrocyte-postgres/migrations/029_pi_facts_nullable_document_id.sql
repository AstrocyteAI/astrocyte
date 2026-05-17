-- M18b post-ship cleanup (2026-05-17): make ``astrocyte_pi_facts``
-- section anchor nullable.
--
-- Pre-M18b, every ``PageIndexFact`` had to belong to a section
-- (``document_id`` + ``line_num`` were NOT NULL, with a composite FK
-- to ``astrocyte_pi_sections``). That coupling was a pragmatic
-- consequence of M12.1 introducing fact-grain bolted to the PageIndex
-- section tree — but Hindsight's equivalent ``memory_units.document_id``
-- is nullable, allowing facts to live directly under a bank with no
-- section anchor.
--
-- M18b promoted Astrocyte's fact recall to Hindsight-parity RRF
-- fusion (see CHANGELOG.md M18b-B1 entry). To complete the Memory
-- Engine alignment, the dataclass was renamed ``PageIndexFact`` →
-- ``MemoryFact`` and ``document_id`` / ``line_num`` made nullable on
-- both the dataclass and the underlying table.
--
-- Anchored facts (typical today, Document/Conversation Engine path):
--   document_id + line_num NOT NULL, FK fires, ON DELETE CASCADE applies
-- Top-level facts (Hindsight-parity, future top-level retain API):
--   document_id + line_num both NULL, FK skipped (PG MATCH SIMPLE default)
--
-- The composite FK constraint remains in place; PostgreSQL's MATCH
-- SIMPLE semantics (the default) treat any NULL column as making the
-- whole constraint pass — so a fact with both columns NULL is allowed.
-- A fact with exactly one column NULL is also allowed by MATCH SIMPLE;
-- application code (PageIndexStore.save_facts) enforces "both-or-
-- neither" via a precondition (see ``store.save_facts``).
--
-- Idempotent: ALTER ... DROP NOT NULL is a no-op if the column is
-- already nullable, so re-running this migration is safe.

ALTER TABLE astrocyte_pi_facts
    ALTER COLUMN document_id DROP NOT NULL;

ALTER TABLE astrocyte_pi_facts
    ALTER COLUMN line_num DROP NOT NULL;

-- The existing ``ix_pi_facts_doc (document_id)`` index continues to
-- work — PostgreSQL B-tree indexes handle NULLs natively (with NULLs
-- placed last by default). No index rebuild needed.
