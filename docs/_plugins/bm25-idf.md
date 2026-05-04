---
title: BM25 with corpus IDF (M9)
---

# BM25 with corpus IDF + length normalisation

Astrocyte's keyword retrieval strategy used to call
`ts_rank_cd(text_fts, plainto_tsquery('english', q), 1)` directly. That
ranks by term frequency and proximity but **doesn't know which terms are
informative**. "Alice", "the", and "novella" all contributed equally to
the score even though "novella" appears in 3 of 36000 documents (highly
informative) while "the" appears in nearly all of them (zero
information). M9 adds proper BM25-with-IDF ranking via two precomputed
materialized views.

## When to enable it

```yaml
# astrocyte.yaml
bm25_idf:
  enabled: true
```

Enable when:

- You're using `vector_store: postgres` (the materialized views are
  Postgres-specific; in_memory and other adapters silently fall through
  to classic ts_rank_cd)
- You've applied migration 013 (`SCHEMA=... ./migrate.sh` covers it)
- You have a refresh strategy in place (see below)

Don't enable when:

- You can't run periodic `REFRESH MATERIALIZED VIEW`. Stale views =
  stale recall results.
- Your corpus is tiny (< ~100 documents). IDF needs corpus statistics
  to be meaningful; in tiny corpora most terms have similar IDF.

## How it works

Two materialized views from migration 013:

### 1. `astrocyte_vectors_bm25` — per-document

```sql
CREATE MATERIALIZED VIEW astrocyte_vectors_bm25 AS
SELECT
    v.id,
    v.bank_id,
    v.text_fts AS text_vector,
    log(1.0 + length(v.text)::float / per_bank_avg_length) AS doc_length_factor
FROM astrocyte_vectors v
WHERE v.forgotten_at IS NULL;
```

`doc_length_factor` is the BM25 length-normalisation term — long
documents need proportionally more matches to score the same as short
ones.

### 2. `astrocyte_term_idf` — per-lexeme

```sql
CREATE MATERIALIZED VIEW astrocyte_term_idf AS
SELECT
    word AS lexeme,
    ndoc AS doc_frequency,
    log((corpus_size - ndoc + 0.5) / (ndoc + 0.5) + 1) AS idf
FROM ts_stat($$ SELECT text_vector FROM astrocyte_vectors_bm25 $$);
```

Inverse document frequency for every lexeme in the corpus. Bake the
formula into the view so query time is an O(1) lookup instead of an
O(corpus) scan per recall.

### Score formula at query time

For an inbound query, `PostgresStore.search_fulltext_bm25` computes:

```
score = ts_rank_cd(text_vector, plainto_tsquery(query), 0)
        × avg_query_idf
        / NULLIF(doc_length_factor, 0)
```

Three components:

- **`ts_rank_cd`** — term-frequency-and-proximity score (already in stock
  Postgres; rewards documents where query terms appear close together)
- **`avg_query_idf`** — average IDF of the query's lexemes that exist in
  the corpus; precomputed once per query via a small JOIN against
  `astrocyte_term_idf` (zero-impact lookup, indexed)
- **`/ doc_length_factor`** — length normalisation; suppresses long-document
  bias

This is **approximate BM25**, not the textbook per-term `Σ tf × idf`
sum. We picked the approximation because the SQL stays simple and the
score's directional behaviour is correct: rare-term queries score
higher, length-normalised. If the bench shows the lift is large enough
to justify going textbook, that's a follow-up.

## Refresh strategy — required reading

The materialized views are NOT updated automatically when memories are
retained. **Without a periodic refresh, BM25 IDF returns no hits for
newly-stored memories.** Three options:

### Option 1: Scheduled refresh (production)

Add a cron / k8s CronJob that runs every N minutes:

```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY astrocyte_vectors_bm25;
REFRESH MATERIALIZED VIEW CONCURRENTLY astrocyte_term_idf;
```

`CONCURRENTLY` keeps reads unblocked during the refresh (slower than
non-concurrent, but production-safe). Refresh `astrocyte_vectors_bm25`
**before** `astrocyte_term_idf` because the IDF view derives from it.

Suggested cadence: 5-15 minutes for chat-style workloads, 1 hour for
batch ingest, daily for archive corpora. Tune based on staleness
tolerance.

### Option 2: Application-level helper

```python
await store.refresh_bm25_views(concurrent=True)  # store: PostgresStore
```

Call this after batched ingest from your application code. Safe from
multiple workers because the underlying `REFRESH CONCURRENTLY` takes a
lock that serialises overlapping refreshes.

### Option 3: After-batch hook (the bench harness pattern)

The LoCoMo bench harness automatically calls `refresh_bm25_views` at
the retain → eval boundary:

```python
# astrocyte/eval/benchmarks/locomo.py
metrics_collector.set_phase("eval")
for store_attr in ("document_store", "vector_store"):
    refresh = getattr(getattr(self.brain._pipeline, store_attr, None),
                      "refresh_bm25_views", None)
    if callable(refresh):
        await refresh()
        break
```

The same pattern works for any "batch retain then read" workflow — wire
the refresh into the phase boundary.

## Per-tenant behaviour

The materialized views are tenant-aware via `astrocyte.tenancy.fq_table`:
each tenant schema has its own `astrocyte_vectors_bm25` and
`astrocyte_term_idf` (a fresh pair created by `migrate.sh` per schema).
Refresh per-tenant when ingesting new data:

```python
from astrocyte.tenancy import use_schema

with use_schema("tenant_acme"):
    await store.refresh_bm25_views(concurrent=True)
```

For multi-tenant deployments running `migrate-all-tenants.sh` for
schema setup, plan a tenant-loop refresh similarly.

## Expected bench impact

Per the LoCoMo dataset analysis:

- **Multi-hop questions** (rely on multiple distinctive terms appearing
  together): **+3-6pt** expected. This is where the IDF weighting most
  shifts ranking — a query like "When did Alice sign the lease?" puts
  much more weight on "Alice" + "lease" than on "When" + "did" + "the".
- **Open-domain questions** (rely on distinctive single terms): **+2-5pt**
  expected. IDF promotes the one document containing the rare term over
  documents with more total but less distinctive matches.
- **Single-hop with paraphrased semantic match**: **negligible change**.
  Already handled well by semantic embeddings; BM25 IDF is mostly
  redundant.
- **Adversarial (no answer exists)**: **no change**. Retrieval ranking
  doesn't fix "the answer isn't there."
- **Temporal**: **slight positive or negative** depending on whether
  temporal queries lean on rare proper nouns ("the Q3 incident") or on
  common temporal words ("when", "recently").

Net overall: **+2-5pt** is realistic. **Not a silver bullet** — the
bench evidence from earlier displacement-noise experiments suggests RRF
fusion can absorb keyword-leg gains. The expected upper bound on what
keyword ranking improvements alone can deliver is bounded by how much
keyword contributes vs. semantic in the final fused ranking.

If the bench delta after enabling `bm25_idf: true` is **< 2pt**, the
honest reading is "approximate BM25 isn't enough; the gain (if any)
needs textbook per-term BM25 in SQL." That's a follow-up decision,
not a v1.

## Trade-offs

| Concern | Approximate BM25 (M9, this) | Textbook per-term BM25 |
|---|---|---|
| Implementation complexity | Two materialized views + 1 lookup | Per-term CTE join, much gnarlier SQL |
| Per-query cost | One extra view scan + one O(1) IDF lookup | One per-term ts_rank lookup × N terms |
| Score precision | Approximate — query-level avg IDF, not per-term | Textbook BM25 |
| Materialized view refresh cost | Same | Same (would still need both views) |
| Bench expected lift | +2-5pt overall | +1-2pt MORE than this — not 10× |

The decision to start with the approximation: the SQL is debuggable and
the materialized-view infrastructure is the same investment regardless
of which formula sits on top. If the bench shows real gains and we want
more, the upgrade path is "rewrite the score CTE" — same indexes, same
refresh strategy.

## Falling back

The flag is **best-effort**, not strict. Stores that don't expose
`search_fulltext_bm25` (e.g. `in_memory`, hypothetical Elasticsearch
adapter) silently use `search_fulltext`. So you can enable
`bm25_idf: true` in a single deployment config that mixes adapter
families across environments — Postgres uses the new path, in-memory
test setups use the old, no special-casing needed.

The new path also has its own per-strategy isolation: if the
materialized-view query throws (refresh in progress with
non-concurrent mode, view doesn't exist yet, etc.) the
`parallel_retrieve` per-strategy except handler catches it and the
keyword strategy returns `[]` — same as any other strategy failure.

## Decision record

This is the M9 "BM25 IDF" item from the Hindsight comparison. Hindsight
runs at 92% on LoCoMo and uses `memory_units_bm25` as a precomputed
materialized view; we now have the equivalent infrastructure. Whether
this single change closes meaningful ground vs. the broader stack
differences (different agentic loop, different chunking, different
query-rewriting) — TBD by the bench in this commit's release notes.
