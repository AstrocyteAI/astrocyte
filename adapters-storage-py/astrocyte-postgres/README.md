# astrocyte-postgres

**PostgreSQL** implementation of the Astrocyte **`VectorStore`** and **`WikiStore`** SPIs ([`provider-spi.md`](../../docs/_plugins/provider-spi.md)). Defaults to **[pgvectorscale](https://github.com/timescale/pgvectorscale)** (DiskANN) for ANN indexing; falls back to **[pgvector](https://github.com/pgvector/pgvector)** (HNSW) when the pgvectorscale binary isn't available, and supports **[VectorChord](https://github.com/tensorchord/VectorChord)** (vchordrq) as an opt-in alternative. See the [ANN backend](#schema-migrations-production) section.

## Install

From the monorepo (with `astrocyte` available):

```bash
cd adapters-storage-py/astrocyte-postgres
uv sync
# or: pip install -e ../../astrocyte-py && pip install -e .
```

Entry point names:

- **`postgres`** (group `astrocyte.vector_stores`) for raw/compiled memory vectors.
- **`postgres`** (group `astrocyte.document_stores`) for BM25 keyword retrieval over the same table.
- **`postgres`** (group `astrocyte.wiki_stores`) for durable wiki pages/revisions/provenance.

## PostgreSQL with Docker

Use the **combined** Compose stack in **[`../../astrocyte-services-py/docker-compose.yml`](../../astrocyte-services-py/docker-compose.yml)** to run **Postgres (pgvector) + the reference REST service** together:

```bash
cd astrocyte-services-py
docker compose up -d
```

For **Postgres only** (no HTTP), start only `postgres`:

```bash
cd astrocyte-services-py
docker compose up -d postgres
```

Default DSN from your host (port **5433** maps to Postgres in the compose file):

```text
postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte
```

## Schema migrations (production)

DDL is shipped as **plain SQL** under [`migrations/`](migrations/) and applied with **`psql`** via [`scripts/migrate.sh`](scripts/migrate.sh) (no Python migration framework).

```bash
export DATABASE_URL='postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte'
cd adapters-storage-py/astrocyte-postgres
./scripts/migrate.sh
```

Requirements: **PostgreSQL 15+** (for `CREATE INDEX CONCURRENTLY IF NOT EXISTS`), **psql** on `PATH`.

After migrations are applied, set **`bootstrap_schema: false`** in `vector_store_config` so the app does not run `CREATE TABLE` / indexes at runtime (see configuration table below). For a **single command** that starts Postgres, runs migrations, then starts the stack with runbook config, use **[`runbook-up.sh`](../../astrocyte-services-py/scripts/runbook-up.sh)** (see **[Runbook](../../astrocyte-services-py/README.md#runbook)**).

**Embedding width:** [`migrations/002_astrocyte_vectors.sql`](migrations/002_astrocyte_vectors.sql) creates `vector(${ASTROCYTE_EMBEDDING_DIMENSIONS:-128})`. That must match **`embedding_dimensions`** in config. For OpenAI `text-embedding-3-small`, run migrations with `ASTROCYTE_EMBEDDING_DIMENSIONS=1536`.

**ANN backend (DiskANN by default; HNSW/VectorChord opt-in):** vector indexes are created with the backend selected by the **`VECTOR_EXTENSION`** env var:

| `VECTOR_EXTENSION` | Backend | Index DDL | When to choose |
|---|---|---|---|
| `pgvectorscale` *(default)* | DiskANN (pgvectorscale) | `USING diskann (embedding vector_cosine_ops) WITH (num_neighbors = 50)` | Default for retain-heavy Astrocyte workloads. Better concurrent-insert throughput than HNSW (no per-page write-lock contention) and better pre-filtered query performance. OSS under the PostgreSQL License with no feature gates |
| `pgvector` | HNSW (pgvector) | `USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)` | Fallback for environments where the pgvectorscale binary isn't available (vanilla Postgres images, restricted builds). Mature, widely deployed |
| `vchord` | VectorChord (vchordrq) | `USING vchordrq (embedding vector_cosine_ops)` | Vendor-cited highest insert throughput. Apache-2.0 OSS. Requires `shared_preload_libraries` change (Postgres restart) |

```bash
# Default (DiskANN) — pgvectorscale binary required.
./scripts/migrate.sh

# HNSW fallback — for environments without pgvectorscale.
VECTOR_EXTENSION=pgvector ./scripts/migrate.sh

# VectorChord — requires the vchord .deb installed AND vchord in
# shared_preload_libraries (Postgres restart). The shipped Dockerfile
# does both; runtime CREATE EXTENSION is opt-in.
VECTOR_EXTENSION=vchord ./scripts/migrate.sh
```

All three backends use the **same operator class** (`vector_cosine_ops`) and accelerate the same query operator (`<=>`), so application code is identical regardless of which backend you choose. Only insert throughput, query latency at high recall, and disk footprint differ.

The choice flips [`migrations/001_extension.sql`](migrations/001_extension.sql) (which `CREATE EXTENSION` fires) and [`migrations/003_indexes.sql`](migrations/003_indexes.sql) + [`migrations/009_entities_trigram_embedding.sql`](migrations/009_entities_trigram_embedding.sql) (which `USING` clause builds the index).

The shipped [`docker/astrocyte-postgres/Dockerfile`](../../docker/astrocyte-postgres/Dockerfile) bakes all three extensions so an operator can switch backends with one env var without rebuilding the image. Caveats:
- Switching backend requires wiping the DB and re-migrating — existing indexes can't be promoted/converted in place.
- `vchord` requires `shared_preload_libraries = 'age,vchord'`; the Dockerfile sets this at first cluster init, but external Postgres images need the operator to add it manually before `CREATE EXTENSION vchord` can succeed.
- The default switch from `pgvector` to `pgvectorscale` (2026-05-06) was driven by the LongMemEval bench observing HNSW per-page write-lock drift (~1.0s → 2.0s/session as the index grew under concurrent retain). DiskANN's graph layout serializes less aggressively under concurrent inserts.

**Custom `table_name`:** The shipped SQL targets **`astrocyte_vectors`**. If you use another table name, copy and adjust the migration files accordingly.

The later migrations add the Hindsight-comparable Postgres substrate around vectors: bank metadata and access grants, lifecycle columns (`retained_at`, `forgotten_at`), durable wiki pages/revisions/provenance, canonical entity/link tables, and normalized temporal facts.

## Configuration

| Constructor / YAML `vector_store_config` | Meaning |
|--------------------------------------------|---------|
| `dsn` | PostgreSQL connection URI (or set `DATABASE_URL` / `ASTROCYTE_PG_DSN`) |
| `table_name` | Table name (default `astrocyte_vectors`; alphanumeric + underscore only) |
| `embedding_dimensions` | Fixed `vector(N)` width; must match your embedding model and the **`vector(N)`** in SQL migrations (default **128**) |
| `bootstrap_schema` | If **`true`** (default), create extension / table / btree index on first use (dev-friendly; no HNSW). If **`false`**, assume **`migrate.sh`** already applied [`migrations/`](migrations/) (production). |

## How this fits `astrocyte_gateway`

1. **`astrocyte-py`** defines the **`VectorStore`** protocol and discovers adapters by **entry point** (`astrocyte.vector_stores`).
2. **`astrocyte-postgres`** registers **`postgres` → `PostgresStore`**. Installing this package makes the name **`postgres`** available to **`resolve_provider()`**.
3. **`astrocyte_gateway/wiring.py`** calls **`resolve_vector_store(config)`**, which loads the class from the entry point and passes **`vector_store_config`** from YAML (or env-only defaults).
4. **`astrocyte_gateway/brain.py`** builds **`Astrocyte`** + **`PipelineOrchestrator`** with that store and your chosen **`llm_provider`** (still **`mock`** unless you configure a real LLM).

Example **`ASTROCYTE_CONFIG_PATH`** snippet:

```yaml
provider_tier: storage
vector_store: postgres
llm_provider: mock
vector_store_config:
  dsn: postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte
  embedding_dimensions: 128
  bootstrap_schema: false
wiki_store: postgres
wiki_store_config:
  dsn: postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte
  bootstrap_schema: false
```

Then run the REST service (from repo layout):

```bash
export ASTROCYTE_CONFIG_PATH=/path/to/that.yaml
cd astrocyte-services-py/astrocyte-gateway-py && uv run astrocyte-gateway-py
```

Or set only env (no YAML file):

```bash
export ASTROCYTE_VECTOR_STORE=postgres
export DATABASE_URL=postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte
# embedding_dimensions default 128 — override via YAML if you add a file
cd astrocyte-services-py/astrocyte-gateway-py && uv sync --extra postgres
```

**Note:** `vector_store_config` for dimensions is only merged from YAML today; for env-only mode, add a small YAML or extend `brain.py` to pass `ASTROCYTE_EMBEDDING_DIMENSIONS` (future improvement).

## Production notes

- **HNSW** parameters (`m`, `ef_construction`) live in [`migrations/003_indexes.sql`](migrations/003_indexes.sql); tune with DBA guidance as load grows.
- **Embedding dimension** must match the **`LLMProvider.embed()`** output used by the pipeline.
- Use **secrets** for `dsn`, not committed YAML.
