# astrocyte-sqlite

Zero-infrastructure storage for Astrocyte: every memory in one SQLite file.
No server, no Docker, no native extensions — `pip install` and retain.

```yaml
# astrocyte.yaml
provider_tier: storage
vector_store: sqlite
vector_store_config:
  path: ~/.local/share/astrocyte/astrocyte.db   # default; or ASTROCYTE_SQLITE_PATH
```

`SqliteStore` satisfies both the `VectorStore` and `DocumentStore` protocols,
so the pipeline's keyword retrieval leg turns on automatically (the same
auto-wire `PostgresStore` gets).

## Behaves like Postgres, deliberately

Semantics match `astrocyte-postgres`, not the in-memory test store, because
Postgres is what users deploy and what every benchmark measures:

| Behaviour | `SqliteStore` / `PostgresStore` |
|---|---|
| `delete` | soft delete (`forgotten_at`); `as_of` time travel still sees it |
| re-storing a deleted id | resurrects it |
| `tags` / `fact_types` filters | items without tags / type are excluded |
| similarity score | cosine, clamped to [0, 1] |
| `search_similar` + `time_range` | not applied (`list_recent_vectors` applies it) |
| batch with a wrong-length vector | whole batch rejected |
| keyword query | English stopwords dropped, remaining terms ANDed, stemmed |

`tests/test_parity_postgres.py` runs identical operations against both
backends and fails on any divergence. Known, accepted differences: keyword
*ranking* (FTS5 `bm25` vs `ts_rank_cd` — the pipeline fuses by rank, and the
match set is identical), stemmer edge cases (Porter vs Snowball), and zero
vectors (score 0.0 here; NaN in pgvector).

## Design

- **Exact cosine in numpy over float32 BLOBs.** pgvector stores float4 too.
  `sqlite-vec` was rejected: it needs `enable_load_extension`, which some Python
  builds (including macOS system Python) omit.
- **FTS5 with a LIKE fallback** for SQLite builds compiled without it.
  `health()` reports which is active.
- **Safe across processes.** An MCP server and short-lived agent-hook
  processes write the same file: WAL journal, `BEGIN IMMEDIATE` writes, busy
  timeout, a connection per call.
- **Embedding dimension is pinned on first write.** Switching embedding models
  against an existing file fails loudly instead of mixing incomparable vectors.
- **Scale.** Exact search is linear in a bank's live memories. Measured on an
  Apple Silicon laptop, median of 15 queries, `limit=50`:

  | memories | dim | vector query | keyword query | file |
  |---:|---:|---:|---:|---:|
  | 1,000 | 384 | 3.7 ms | 3.5 ms | 2 MB |
  | 10,000 | 384 | 23 ms | 19 ms | 21 MB |
  | 50,000 | 384 | 116 ms | 95 ms | 107 MB |
  | 10,000 | 1536 | 68 ms | 27 ms | 83 MB |
  | 50,000 | 1536 | 420 ms | 143 ms | 414 MB |

  Use the embedding model's native width: `local_embeddings` with
  `pad_to: null` gives bge-small's 384 dims. Zero-padding to 1536 (needed only
  for Postgres's fixed `vector(1536)` column) costs ~4x in time and disk and
  changes no similarity. Keyword figures are pessimistic: the synthetic corpus
  puts the query terms in nearly every document.
- **Identifiers sort bytewise.** `list_vectors` orders by `id` in byte order.
  Postgres orders by the database collation; the two agree for the lowercase
  UUIDs Astrocyte generates, but could differ for arbitrary mixed-case ids.
