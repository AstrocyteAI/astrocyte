# Astrocyte services (Python)

Optional Python packages beside the core [`astrocyte-py`](../astrocyte-py/) library:

| Package | Role |
|---------|------|
| [`astrocyte-gateway-py/`](astrocyte-gateway-py/README.md) | Standalone **HTTP gateway** (`astrocyte.yaml` + optional `mip.yaml`). **CLI:** `astrocyte-gateway-py`. **Python module:** `astrocyte_gateway`. Optional **`[pgvector]`** and **`[age]`** extras install [`astrocyte-pgvector`](../adapters-storage-py/astrocyte-pgvector/README.md) and `astrocyte-age` for the reference Postgres memory stack. |
| [`adapters-storage-py/astrocyte-pgvector/`](../adapters-storage-py/astrocyte-pgvector/README.md) | **`pgvector`** [`VectorStore`](../docs/_plugins/provider-spi.md) for PostgreSQL + [pgvector](https://github.com/pgvector/pgvector). **Schema:** SQL under [`migrations/`](../adapters-storage-py/astrocyte-pgvector/migrations/) + [`migrate.sh`](../adapters-storage-py/astrocyte-pgvector/scripts/migrate.sh) (`psql`, no Python migrator). |

**Docker:** [`docker-compose.yml`](docker-compose.yml) in this directory runs **Postgres with pgvector + Apache AGE + `astrocyte-gateway-py`**. Copy **[`.env.example`](.env.example)** to `.env` to override Postgres credentials, ports, and REST settings. Run from here: `docker compose up --build`, or from the repo root: `docker compose -f astrocyte-services-py/docker-compose.yml --env-file astrocyte-services-py/.env up --build`. Details: [`astrocyte-gateway-py/README.md`](astrocyte-gateway-py/README.md).

---

## Runbook

Use this for a **production-shaped** local or demo deploy: SQL migrations (including **HNSW**), then **`astrocyte-gateway-py`** with **`bootstrap_schema: false`** ([`config.runbook.example.yaml`](config.runbook.example.yaml)). This is the default Hindsight-informed reference stack: one PostgreSQL instance provides dense vectors (pgvector), durable wiki pages/revisions/provenance, entity graph traversal (Apache AGE with SQL-owned entity/link truth), normalized temporal facts, bank/access metadata, and PgQueuer-backed async memory tasks.

### One command

From **`astrocyte-services-py/`** (optional: `cp .env.example .env` and edit first):

```bash
./scripts/runbook-up.sh
```

From the **repository root**:

```bash
./astrocyte-services-py/scripts/runbook-up.sh
```

This script: starts **Postgres**, waits until it accepts connections, runs [`adapters-storage-py/astrocyte-pgvector/scripts/migrate.sh`](../adapters-storage-py/astrocyte-pgvector/scripts/migrate.sh) against **`127.0.0.1:${POSTGRES_PUBLISH_PORT}`**, then brings up **`docker-compose.yml` + [`docker-compose.runbook.yml`](docker-compose.runbook.yml)**. Requires **`psql`** on your PATH and Postgres **15+** (for concurrent index creation in [`003_indexes.sql`](../adapters-storage-py/astrocyte-pgvector/migrations/003_indexes.sql)).

If the password cannot be used in a constructed URL, set **`MIGRATE_DATABASE_URL`** in `.env` (see [`.env.example`](.env.example)).

### Verify

- **HTTP:** `GET http://127.0.0.1:${ASTROCYTE_HTTP_PUBLISH_PORT:-8080}/health` (default **8080**).
- **OpenAPI:** `http://127.0.0.1:8080/docs` (adjust port if overridden).

### Debugging

- **`/live` vs `/health`:** `GET /live` only checks the process; `GET /health` also checks the vector store (PostgreSQL). If `/live` works but `/health` fails, inspect the API and DB (see below).
- **Response body:** `curl -sS http://127.0.0.1:8080/health` (omit `curl -f`) so a non-2xx response still prints JSON `detail`.
- **Logs:** `docker compose logs astrocyte-gateway-py` (add `-f` to follow).
- **Effective `DATABASE_URL` in the container:** `docker compose exec astrocyte-gateway-py printenv DATABASE_URL` — must use hostname **`postgres`**, not `127.0.0.1`, for Compose networking.
- **Resolved Compose config:** `docker compose -f docker-compose.yml -f docker-compose.runbook.yml config` (check `environment` for `astrocyte-gateway-py`).
- **Postgres from the API container:** `docker compose exec postgres psql -U astrocyte -d astrocyte -c 'SELECT 1'` (adjust user/db to match `.env`), or run a short `asyncio` + `psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"])` snippet inside `astrocyte-gateway-py` to mirror what the app uses.

### Manual steps (same as the script)

If you prefer not to use [`scripts/runbook-up.sh`](scripts/runbook-up.sh):

1. `docker compose up -d postgres` and wait until **healthy** (Compose does not run host-side `migrate.sh`; Postgres must be listening on the host port before migrations).
2. `export DATABASE_URL=postgresql://USER:PASS@127.0.0.1:POSTGRES_PUBLISH_PORT/DB` then `../adapters-storage-py/astrocyte-pgvector/scripts/migrate.sh`
3. `docker compose -f docker-compose.yml -f docker-compose.runbook.yml up --build`

Or from the repository root:

```bash
docker compose -f astrocyte-services-py/docker-compose.yml -f astrocyte-services-py/docker-compose.runbook.yml --env-file astrocyte-services-py/.env up --build
```

### Quick path (skip migrations runbook)

`docker compose up --build` alone uses in-app bootstrap DDL (no **HNSW** from SQL). For **ANN indexes** and migrations owning the schema, use **`./scripts/runbook-up.sh`** or the manual steps above.
