-- BM25 / IDF materialized views for proper full-text ranking.
--
-- Background:
--   ``search_fulltext`` (the path Astrocyte takes today) ranks via
--   ``ts_rank_cd(text_fts, plainto_tsquery, 1)`` — Postgres's "cover
--   density" function. That returns a tf-and-proximity score but DOES
--   NOT incorporate inverse document frequency. Rare query terms
--   ("Alice", "novella", "Q3 incident") are weighted the same as common
--   ones ("the", "what", "how"), so multi-hop and open-domain questions
--   that hinge on distinctive proper nouns / dates lose ranking signal.
--
--   This migration adds two materialized views that together let us
--   compute approximate BM25 at query time:
--
--   1. ``astrocyte_vectors_bm25`` — per-document text_vector + a
--      length-normalisation factor. The MV insulates the BM25 query
--      path from the live ``astrocyte_vectors`` table, so heavy
--      ranking queries don't compete with retain writes for buffer
--      cache.
--
--   2. ``astrocyte_term_idf`` — per-lexeme document-frequency aggregate
--      (bank-scoped) used to compute IDF at query time without scanning
--      the corpus on every recall.
--
--   Both views are refreshed manually via ``REFRESH MATERIALIZED VIEW
--   CONCURRENTLY`` (which requires the unique indexes below). Suggested
--   refresh cadence: after batched retain in benchmarks, or hourly /
--   on-percent-change in production. ``PostgresStore.refresh_bm25_views()``
--   provides a one-call helper.

-- ---------------------------------------------------------------------------
-- Per-document materialized view
-- ---------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS astrocyte_vectors_bm25 AS
SELECT
    v.id,
    v.bank_id,
    v.text_fts AS text_vector,
    -- BM25 length-normalisation: log(1 + |D| / avgdl_per_bank). When the
    -- bank has only one (or zero) documents the divisor would be zero;
    -- COALESCE to the document's own length keeps the factor at log(2)
    -- in that edge case rather than producing a NaN.
    log(1.0 + length(v.text)::float / GREATEST(
        COALESCE(
            (
                SELECT avg(length(text))::float
                  FROM astrocyte_vectors v2
                 WHERE v2.bank_id = v.bank_id
                   AND v2.forgotten_at IS NULL
            ),
            length(v.text)::float
        ),
        1.0
    )) AS doc_length_factor
FROM astrocyte_vectors v
WHERE v.forgotten_at IS NULL;

-- Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS astrocyte_vectors_bm25_id_idx
    ON astrocyte_vectors_bm25 (id);

-- Hot-path: filter by bank_id then full-text match.
CREATE INDEX IF NOT EXISTS astrocyte_vectors_bm25_bank_idx
    ON astrocyte_vectors_bm25 (bank_id);

CREATE INDEX IF NOT EXISTS astrocyte_vectors_bm25_text_vector_idx
    ON astrocyte_vectors_bm25 USING GIN (text_vector);

-- ---------------------------------------------------------------------------
-- Per-lexeme term-IDF materialized view
-- ---------------------------------------------------------------------------
-- Uses ``ts_stat`` to enumerate every lexeme present in the BM25 view's
-- tsvectors and the number of documents containing each. The IDF formula
-- below is the standard BM25 form:
--   IDF(t) = log((N - df(t) + 0.5) / (df(t) + 0.5) + 1)
-- We bake it directly into the view so query time is a simple lookup
-- instead of a per-recall recomputation.
--
-- Note: ``ts_stat`` is not bank-aware — it scans the entire MV. For most
-- workloads this is fine because the IDF distribution shifts slowly. If a
-- deployment ever needs strict per-bank IDF, swap this view for a
-- per-bank version (more refresh cost, same query shape).

CREATE MATERIALIZED VIEW IF NOT EXISTS astrocyte_term_idf AS
WITH corpus_size AS (
    SELECT count(*)::float AS n FROM astrocyte_vectors_bm25
),
term_doc_freq AS (
    SELECT word, ndoc::float AS df
      FROM ts_stat($$ SELECT text_vector FROM astrocyte_vectors_bm25 $$)
)
SELECT
    t.word                                        AS lexeme,
    t.df                                          AS doc_frequency,
    log((cs.n - t.df + 0.5) / (t.df + 0.5) + 1.0) AS idf
FROM term_doc_freq t
CROSS JOIN corpus_size cs;

CREATE UNIQUE INDEX IF NOT EXISTS astrocyte_term_idf_lexeme_idx
    ON astrocyte_term_idf (lexeme);
