-- Migration 030: page_start / page_end on astrocyte_document_nodes
--
-- Adds optional page-coordinate columns to support PDF documents.
-- Markdown nodes leave these NULL (they use line_start / line_end).
-- PDF nodes populated by PdfTreeBuilder (future) set page_start / page_end;
-- the markitdown path produces ## Page N headings so line_start tracks
-- page position via heading line numbers until the native builder ships.
--
-- Additive — no backfill required. Existing markdown nodes get NULL.

ALTER TABLE astrocyte_document_nodes
    ADD COLUMN IF NOT EXISTS page_start int,
    ADD COLUMN IF NOT EXISTS page_end   int;
