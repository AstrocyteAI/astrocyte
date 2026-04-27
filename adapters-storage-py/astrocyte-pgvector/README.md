# astrocyte-pgvector

**PostgreSQL + [pgvector](https://github.com/pgvector/pgvector)** implementation of the Astrocyte **`VectorStore`** and **`WikiStore`** SPIs ([`provider-spi.md`](../../docs/_plugins/provider-spi.md)).

## Install

From the monorepo (with `astrocyte` available):

```bash
cd adapters-storage-py/astrocyte-pgvector
uv sync
# or: pip install -e ../../astrocyte-py && pip install -e .
```

Entry point names:

- **`pgvector`** (group `astrocyte.vector_stores`) for raw/compiled memory vectors.
- **`pgvector`** (group `astrocyte.wiki_stores`) for durable wiki pages/revisions/provenance.

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
cd adapters-storage-py/astrocyte-pgvector
./scripts/migrate.sh
```

Requirements: **PostgreSQL 15+** (for `CREATE INDEX CONCURRENTLY IF NOT EXISTS`), **psql** on `PATH`.

After migrations are applied, set **`bootstrap_schema: false`** in `vector_store_config` so the app does not run `CREATE TABLE` / indexes at runtime (see configuration table below). For a **single command** that starts Postgres, runs migrations, then starts the stack with runbook config, use **[`runbook-up.sh`](../../astrocyte-services-py/scripts/runbook-up.sh)** (see **[Runbook](../../astrocyte-services-py/README.md#runbook)**).

**Embedding width:** [`migrations/002_astrocyte_vectors.sql`](migrations/002_astrocyte_vectors.sql) creates `vector(${ASTROCYTE_EMBEDDING_DIMENSIONS:-128})`. That must match **`embedding_dimensions`** in config. For OpenAI `text-embedding-3-small`, run migrations with `ASTROCYTE_EMBEDDING_DIMENSIONS=1536`.

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
2. **`astrocyte-pgvector`** registers **`pgvector` → `PgVectorStore`**. Installing this package makes the name **`pgvector`** available to **`resolve_provider()`**.
3. **`astrocyte_gateway/wiring.py`** calls **`resolve_vector_store(config)`**, which loads the class from the entry point and passes **`vector_store_config`** from YAML (or env-only defaults).
4. **`astrocyte_gateway/brain.py`** builds **`Astrocyte`** + **`PipelineOrchestrator`** with that store and your chosen **`llm_provider`** (still **`mock`** unless you configure a real LLM).

Example **`ASTROCYTE_CONFIG_PATH`** snippet:

```yaml
provider_tier: storage
vector_store: pgvector
llm_provider: mock
vector_store_config:
  dsn: postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte
  embedding_dimensions: 128
  bootstrap_schema: false
wiki_store: pgvector
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
export ASTROCYTE_VECTOR_STORE=pgvector
export DATABASE_URL=postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte
# embedding_dimensions default 128 — override via YAML if you add a file
cd astrocyte-services-py/astrocyte-gateway-py && uv sync --extra pgvector
```

**Note:** `vector_store_config` for dimensions is only merged from YAML today; for env-only mode, add a small YAML or extend `brain.py` to pass `ASTROCYTE_EMBEDDING_DIMENSIONS` (future improvement).

## Production notes

- **HNSW** parameters (`m`, `ef_construction`) live in [`migrations/003_indexes.sql`](migrations/003_indexes.sql); tune with DBA guidance as load grows.
- **Embedding dimension** must match the **`LLMProvider.embed()`** output used by the pipeline.
- Use **secrets** for `dsn`, not committed YAML.
