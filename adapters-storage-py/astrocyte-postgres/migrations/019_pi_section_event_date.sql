-- M11.1: structured event-time extraction at the section level.
--
-- Per-section LLM extraction at retain time emits an OCCURRED START
-- (and optional END) for the most-prominent event the section
-- describes. Mirrors Hindsight's per-fact ``occurred_start`` /
-- ``occurred_end`` columns on ``memory_units``.
--
-- Why this is separate from ``session_date`` (already on the row):
-- ``session_date`` is when the conversation session happened. The
-- discussed event may have happened earlier ("Yesterday I went to
-- the doctor" in a session dated May 8 → session_date=May 8 but
-- the event itself happened on May 7). ``temporal_arithmetic.find_event_date``
-- uses the event date when present, falling back to session_date
-- when the LLM didn't extract one.
--
-- Indexed for the temporal recall strategy's date-range filter.

ALTER TABLE astrocyte_pi_sections
    ADD COLUMN IF NOT EXISTS occurred_start TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS occurred_end   TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_pi_sections_occurred_start
    ON astrocyte_pi_sections (occurred_start)
    WHERE occurred_start IS NOT NULL;
