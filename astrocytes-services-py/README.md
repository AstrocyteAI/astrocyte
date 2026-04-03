# Astrocytes services (Python)

Optional Python packages beside the core [`astrocytes-py`](../astrocytes-py/) library:

| Package | Role |
|---------|------|
| [`astrocytes-rest/`](astrocytes-rest/README.md) | Reference **REST** HTTP server. **CLI:** `astrocytes-rest`. **Python module:** `astrocytes_rest`. Optional **`[pgvector]`** extra installs [`astrocytes-pgvector`](astrocytes-pgvector/README.md) for durable vectors. |
| [`astrocytes-pgvector/`](astrocytes-pgvector/README.md) | **`pgvector`** [`VectorStore`](../docs/04-provider-spi.md) for PostgreSQL + [pgvector](https://github.com/pgvector/pgvector). **Schema:** SQL under [`astrocytes-pgvector/migrations/`](astrocytes-pgvector/migrations/) + [`scripts/migrate.sh`](astrocytes-pgvector/scripts/migrate.sh) (`psql`, no Python migrator). |

**Docker:** [`docker-compose.yml`](docker-compose.yml) in this directory runs **Postgres (pgvector) + `astrocytes-rest`**. Copy **[`.env.example`](.env.example)** to `.env` to override Postgres credentials, ports, and REST settings. Run from here: `docker compose up --build`, or from the repo root: `docker compose -f astrocytes-services-py/docker-compose.yml --env-file astrocytes-services-py/.env up --build`. Details: [`astrocytes-rest/README.md`](astrocytes-rest/README.md).

---

## Runbook

Use this for a **production-shaped** local or demo deploy (Postgres, SQL migrations, REST): apply SQL first, then run the API **without** runtime DDL (`bootstrap_schema: false`).

### 1. Environment

From **`astrocytes-services-py/`**:

```bash
cp .env.example .env
# Edit .env: set POSTGRES_* secrets and ports if needed.
```

### 2. Start Postgres only

```bash
docker compose up -d postgres
```

Wait until the service is **healthy** (`docker compose ps`).

### 3. Apply SQL migrations

Use the same credentials and host port as in `.env` (defaults: user/password/db **`astrocytes`**, host port **`5433`**):

```bash
set -a
. ./.env
set +a
export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PUBLISH_PORT:-5433}/${POSTGRES_DB}"
./astrocytes-pgvector/scripts/migrate.sh
```

If the password contains characters that break URLs, set **`DATABASE_URL`** explicitly in `.env` and `export DATABASE_URL` after sourcing, or paste the full URI for this step.

Requirements: **`psql`** on your PATH; Postgres **15+** (for concurrent index creation in [`003_indexes.sql`](astrocytes-pgvector/migrations/003_indexes.sql)).

### 4. Start the full stack (no in-app DDL)

Use the optional Compose override and example config ([`config.runbook.example.yaml`](config.runbook.example.yaml) sets **`bootstrap_schema: false`**):

```bash
docker compose -f docker-compose.yml -f docker-compose.runbook.yml up --build
```

Or from the repository root:

```bash
docker compose -f astrocytes-services-py/docker-compose.yml -f astrocytes-services-py/docker-compose.runbook.yml --env-file astrocytes-services-py/.env up --build
```

### 5. Verify

- **HTTP:** `GET http://127.0.0.1:${ASTROCYTES_HTTP_PUBLISH_PORT:-8080}/health` (default **8080**).
- **OpenAPI:** `http://127.0.0.1:8080/docs` (adjust port if overridden).

### Quick path (skip migrations runbook)

`docker compose up --build` alone uses in-app bootstrap DDL (no HNSW from SQL). For **ANN indexes** and strict “migrations own the schema” behavior, follow steps 2–4 above.
