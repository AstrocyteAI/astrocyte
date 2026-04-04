# Astrocytes REST reference server (Python)

Optional **REST** front end for [`astrocytes-py`](../../astrocytes-py/). It embeds the `Astrocyte` API behind HTTP so you can try the framework without writing a host app.

**Defaults:** Tier 1 providers resolve from **`Astrocytes` config** (built-in **`in_memory`** vector / graph / document stores and **`mock`** LLM unless you override). Same pattern as conformance tests when defaults are used. Data is **not** durable with in-memory backends. This package is **not** production-ready as shipped; see below.

**Production and operations:** The full checklist, architecture context, and documentation of **`astrocytes-rest`** as a **starting point** for a hardened deployment are in **[`docs/_end-user/production-grade-http-service.md`](../../docs/_end-user/production-grade-http-service.md)** (especially §3 checklist and §4 reference REST service).

## Run locally

Install `astrocytes` first, then this package:

```bash
cd /path/to/astrocytes/astrocytes-py && pip install -e .
cd /path/to/astrocytes/astrocytes-services-py/astrocytes-rest && pip install -e .
ASTROCYTES_HOST=0.0.0.0 ASTROCYTES_PORT=8080 astrocytes-rest
```

Or:

```bash
uv run --directory astrocytes-services-py/astrocytes-rest astrocytes-rest
```

**uv:** `pyproject.toml` pins **`[tool.uv.sources]`** so `astrocytes` resolves from **`../../astrocytes-py`** (editable). Run `uv sync` from **`astrocytes-services-py/astrocytes-rest/`**. Plain **`pip`** users should `pip install -e ../../astrocytes-py` first, then install this package.

**PostgreSQL (pgvector):** Install the optional adapter (`uv sync --extra pgvector`) and run Postgres. The fastest path is **Docker Compose** at **[`../docker-compose.yml`](../docker-compose.yml)** (repo **`astrocytes-services-py/`**), which starts **Postgres + this service** together. For Postgres only on the host, see [`astrocytes-pgvector`](../astrocytes-pgvector/README.md).

Then set `vector_store: pgvector` in YAML or `ASTROCYTES_VECTOR_STORE=pgvector`, and pass a DSN via `vector_store_config.dsn` or `DATABASE_URL` / `ASTROCYTES_PG_DSN`.

## Configuration

**YAML:** Set `ASTROCYTES_CONFIG_PATH` to a file (see [`config.example.yaml`](./config.example.yaml)). Required fields include `provider_tier: storage` for the Tier 1 REST path. Provider keys (`vector_store`, `llm_provider`, optional `graph_store`, `document_store`) name an **entry point** from `astrocytes-py` or a **`package.module:ClassName`** import path (see `astrocytes._discovery.resolve_provider`).

**Environment (HTTP process):**

| Variable | Meaning |
|----------|---------|
| `ASTROCYTES_HOST` | Bind address (default `127.0.0.1`). Use `0.0.0.0` in containers. |
| `ASTROCYTES_PORT` | Port (default `8080`). |
| `ASTROCYTES_CONFIG_PATH` | Optional YAML for full `AstrocyteConfig` (profiles, policy, provider names, `*_config` kwargs). |
| `ASTROCYTES_VECTOR_STORE` | Override `vector_store` when no YAML file is used (default `in_memory`). |
| `ASTROCYTES_LLM_PROVIDER` | Override `llm_provider` when no YAML file is used (default `mock`). |
| `ASTROCYTES_GRAPH_STORE` | Optional graph store name. |
| `ASTROCYTES_DOCUMENT_STORE` | Optional document store name. |
| `DATABASE_URL` / `ASTROCYTES_PG_DSN` | When using **`pgvector`**, connection URI for PostgreSQL (see [`astrocytes-pgvector`](../astrocytes-pgvector/README.md)); can be omitted if `dsn` is set in YAML `vector_store_config`. |

If **`ASTROCYTES_CONFIG_PATH`** is **not** set, the process uses an empty `AstrocyteConfig` plus the env overrides above and applies **dev-style** defaults (PII off, access control off), matching the previous reference behavior. Install **`astrocytes-pgvector`** (`uv sync --extra pgvector`) before selecting `pgvector` as the vector store.

## Identity

Send optional header **`X-Astrocytes-Principal`** (for example `user:alice`) when `access_control` is enabled in config and grants are configured on the `Astrocyte` instance. **This header is not authenticated** in the reference server; do not rely on it for production security (see [`docs/_end-user/production-grade-http-service.md`](../../docs/_end-user/production-grade-http-service.md)).

## HTTP API (summary)

| Method | Path | Body (JSON) |
|--------|------|-------------|
| `GET` | `/live` | - (process up; no DB check) |
| `GET` | `/health` | - (includes vector store / DB) |
| `POST` | `/v1/retain` | `content`, `bank_id`; optional `metadata`, `tags` |
| `POST` | `/v1/recall` | `query`; `bank_id` or `banks`; optional `max_results`, `max_tokens`, `tags` |
| `POST` | `/v1/reflect` | `query`, `bank_id`; optional `max_tokens`, `include_sources` |
| `POST` | `/v1/forget` | `bank_id`; optional `memory_ids`, `tags` |

OpenAPI docs: `/docs` when the HTTP service is running.

## Docker

### REST + Postgres (pgvector)

From **`astrocytes-services-py/`** (or pass `-f` from the repo root):

```bash
cp .env.example .env   # optional: set secrets and ports
docker compose up --build
```

Defaults expose **API** on **8080** and **Postgres** on **5433**; override with **`ASTROCYTES_HTTP_PUBLISH_PORT`** and **`POSTGRES_PUBLISH_PORT`** in [`.env.example`](../.env.example). On the host, use `postgresql://USER:PASSWORD@127.0.0.1:POSTGRES_PUBLISH_PORT/DB` matching your `.env`. Inside Compose, `DATABASE_URL` points at the `postgres` service (see `.env.example`).

**Full deploy (one command):** from [`astrocytes-services-py/`](../), run **`./scripts/runbook-up.sh`** (see **[Runbook](../README.md#runbook)**).

### REST image only

From the **repository root** (`astrocytes/`):

```bash
docker build -f astrocytes-services-py/astrocytes-rest/Dockerfile -t astrocytes-rest .
docker run --rm -p 8080:8080 astrocytes-rest
```

Then `GET http://localhost:8080/health`.
