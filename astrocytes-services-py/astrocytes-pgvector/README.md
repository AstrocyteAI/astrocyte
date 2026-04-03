# astrocytes-pgvector

**PostgreSQL + [pgvector](https://github.com/pgvector/pgvector)** implementation of the Astrocytes **`VectorStore`** SPI ([`04-provider-spi.md`](../../docs/04-provider-spi.md)).

## Install

From the monorepo (with `astrocytes` available):

```bash
cd astrocytes-services-py/astrocytes-pgvector
uv sync
# or: pip install -e ../../astrocytes-py && pip install -e .
```

Entry point name: **`pgvector`** (group `astrocytes.vector_stores`).

## PostgreSQL with Docker

Use the **combined** Compose stack in **[`../docker-compose.yml`](../docker-compose.yml)** (directory **`astrocytes-services-py/`**) to run **Postgres (pgvector) + the reference REST service** together:

```bash
cd astrocytes-services-py
docker compose up -d
```

For **Postgres only** (no HTTP), start only `postgres`:

```bash
cd astrocytes-services-py
docker compose up -d postgres
```

Default DSN from your host (port **5433** maps to Postgres in the compose file):

```text
postgresql://astrocytes:astrocytes@127.0.0.1:5433/astrocytes
```

## Configuration

| Constructor / YAML `vector_store_config` | Meaning |
|--------------------------------------------|---------|
| `dsn` | PostgreSQL connection URI (or set `DATABASE_URL` / `ASTROCYTES_PG_DSN`) |
| `table_name` | Table name (default `astrocytes_vectors`; alphanumeric + underscore only) |
| `embedding_dimensions` | Fixed `vector(N)` width; must match your embedding model (default **128** to match the built-in mock LLM in tests) |

The store creates the `vector` extension and table on first use (`CREATE IF NOT EXISTS`).

## How this fits `astrocytes_rest`

1. **`astrocytes-py`** defines the **`VectorStore`** protocol and discovers adapters by **entry point** (`astrocytes.vector_stores`).
2. **`astrocytes-pgvector`** registers **`pgvector` → `PgVectorStore`**. Installing this package makes the name **`pgvector`** available to **`resolve_provider()`**.
3. **`astrocytes_rest/wiring.py`** calls **`resolve_vector_store(config)`**, which loads the class from the entry point and passes **`vector_store_config`** from YAML (or env-only defaults).
4. **`astrocytes_rest/brain.py`** builds **`Astrocyte`** + **`PipelineOrchestrator`** with that store and your chosen **`llm_provider`** (still **`mock`** unless you configure a real LLM).

Example **`ASTROCYTES_CONFIG_PATH`** snippet:

```yaml
provider_tier: storage
vector_store: pgvector
llm_provider: mock
vector_store_config:
  dsn: postgresql://astrocytes:astrocytes@127.0.0.1:5433/astrocytes
  embedding_dimensions: 128
```

Then run the REST service (from repo layout):

```bash
export ASTROCYTES_CONFIG_PATH=/path/to/that.yaml
cd astrocytes-services-py/astrocytes-rest && uv run astrocytes-rest
```

Or set only env (no YAML file):

```bash
export ASTROCYTES_VECTOR_STORE=pgvector
export DATABASE_URL=postgresql://astrocytes:astrocytes@127.0.0.1:5433/astrocytes
# embedding_dimensions default 128 — override via YAML if you add a file
cd astrocytes-services-py/astrocytes-rest && uv sync --extra pgvector
```

**Note:** `vector_store_config` for dimensions is only merged from YAML today; for env-only mode, add a small YAML or extend `brain.py` to pass `ASTROCYTES_EMBEDDING_DIMENSIONS` (future improvement).

## Production notes

- Run **migrations** and **index tuning** (e.g. `ivfflat` / `hnsw`) outside this MVP as load grows ([`04-provider-spi.md`](../../docs/04-provider-spi.md), DBA guides).
- **Embedding dimension** must match the **`LLMProvider.embed()`** output used by the pipeline.
- Use **secrets** for `dsn`, not committed YAML.
