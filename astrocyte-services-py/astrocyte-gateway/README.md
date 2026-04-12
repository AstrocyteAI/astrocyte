# Astrocyte gateway (Python)

Optional **standalone HTTP** service for [`astrocyte-py`](../../astrocyte-py/): one process exposes `retain` / `recall` / `reflect` / `forget` over REST. **Behavior is not hard-coded in Python** — you drive almost everything through **`astrocyte.yaml`** (`AstrocyteConfig`: providers, policy, `recall_authority`, banks, access grants, …) and optionally **`mip.yaml`** via **`mip_config_path`** in that same YAML for Memory Intent Protocol routing.

**Defaults:** Tier 1 providers resolve from config (built-in **`in_memory`** stores and **`mock`** LLM unless you override). Data is **not** durable with in-memory backends. The gateway is a **convenience and starting point** for deployments; treat production hardening as mandatory — see below.

**Production and operations:** Checklist and ops context are in **[`docs/_end-user/production-grade-http-service.md`](../../docs/_end-user/production-grade-http-service.md)** (especially §3 and §4 gateway section).

### Quick start (official image from GHCR)

After a **`v*`** tag, CI publishes **`ghcr.io/<github-owner>/<repo>/astrocyte-gateway:<tag>`** and **`:latest`** (see **[`RELEASE.md`](./RELEASE.md)**). Example:

```bash
docker pull ghcr.io/astrocyteai/astrocyte/astrocyte-gateway:latest
docker run --rm -p 8080:8080 \
  -e ASTROCYTE_HOST=0.0.0.0 \
  -e ASTROCYTE_CONFIG_PATH=/config/astrocyte.yaml \
  -v /abs/path/to/examples/tier1-minimal/astrocyte.yaml:/config/astrocyte.yaml:ro \
  ghcr.io/astrocyteai/astrocyte/astrocyte-gateway:latest
```

Replace **`astrocyteai/astrocyte`** with your GitHub **`owner/repo`**. For **pgvector**, add **`DATABASE_URL`** (and use a config that sets `vector_store: pgvector`). Copy **[`examples/`](./examples/)** as a starting point.

## Run locally

Install `astrocyte` first, then this package:

```bash
cd /path/to/astrocyte/astrocyte-py && pip install -e .
cd /path/to/astrocyte/astrocyte-services-py/astrocyte-gateway && pip install -e .
ASTROCYTE_HOST=0.0.0.0 ASTROCYTE_PORT=8080 astrocyte-gateway
```

Or:

```bash
uv run --directory astrocyte-services-py/astrocyte-gateway astrocyte-gateway
```

**uv:** `pyproject.toml` pins **`[tool.uv.sources]`** so `astrocyte` resolves from **`../../astrocyte-py`** (editable). Run `uv sync` from **`astrocyte-services-py/astrocyte-gateway/`**. Plain **`pip`** users should `pip install -e ../../astrocyte-py` first, then install this package.

**PostgreSQL (pgvector):** Install the optional adapter (`uv sync --extra pgvector`) and run Postgres. The fastest path is **Docker Compose** at **[`../docker-compose.yml`](../docker-compose.yml)** (repo **`astrocyte-services-py/`**), which starts **Postgres + this service** together. For Postgres only on the host, see [`astrocyte-pgvector`](../../adapters-storage-py/astrocyte-pgvector/README.md).

Then set `vector_store: pgvector` in YAML or `ASTROCYTE_VECTOR_STORE=pgvector`, and pass a DSN via `vector_store_config.dsn` or `DATABASE_URL` / `ASTROCYTE_PG_DSN`.

## Configuration

**`astrocyte.yaml`:** Set `ASTROCYTE_CONFIG_PATH` to your main config file (see [`config.example.yaml`](./config.example.yaml)). Required fields include `provider_tier: storage` for the Tier 1 pipeline. Provider keys (`vector_store`, `llm_provider`, optional `graph_store`, `document_store`) name an **entry point** from `astrocyte-py` or a **`package.module:ClassName`** import path (see `astrocyte._discovery.resolve_provider`).

**`mip.yaml` (optional):** Set **`mip_config_path`** in `astrocyte.yaml` to a separate file to configure MIP routing (declarative memory intent). The gateway loads it through the same `AstrocyteConfig` as library mode — no duplicate MIP stack.

**Environment (HTTP process):**

| Variable | Meaning |
|----------|---------|
| `ASTROCYTE_HOST` | Bind address (default `127.0.0.1`). Use `0.0.0.0` in containers. |
| `ASTROCYTE_PORT` | Port (default `8080`). |
| `ASTROCYTE_CONFIG_PATH` | Optional YAML for full `AstrocyteConfig` (profiles, policy, provider names, `*_config` kwargs). |
| `ASTROCYTE_VECTOR_STORE` | Override `vector_store` when no YAML file is used (default `in_memory`). |
| `ASTROCYTE_LLM_PROVIDER` | Override `llm_provider` when no YAML file is used (default `mock`). |
| `ASTROCYTE_GRAPH_STORE` | Optional graph store name. |
| `ASTROCYTE_DOCUMENT_STORE` | Optional document store name. |
| `DATABASE_URL` / `ASTROCYTE_PG_DSN` | When using **`pgvector`**, connection URI for PostgreSQL (see [`astrocyte-pgvector`](../../adapters-storage-py/astrocyte-pgvector/README.md)); can be omitted if `dsn` is set in YAML `vector_store_config`. |
| `ASTROCYTE_MAX_REQUEST_BODY_BYTES` | If set to a positive integer, reject requests whose **`Content-Length`** exceeds it (**413**). Unset = no limit (dev default). |
| `ASTROCYTE_CORS_ORIGINS` | Comma-separated allowed origins for **browser** `fetch` (e.g. `https://app.example.com`). Unset = CORS middleware not added (same-origin / server-to-server only). |
| `ASTROCYTE_ADMIN_TOKEN` | If set, **`GET /v1/admin/*`** requires header **`X-Admin-Token`** with the same value (use behind TLS; rotate like any secret). Unset = admin routes behave like other API routes (auth mode only). |
| `ASTROCYTE_LOG_FORMAT` | Set to **`json`** for JSON lines on loggers `astrocyte_gateway` / `astrocyte_gateway.access` (aligns with §3.6 observability in production docs). |
| `ASTROCYTE_LOG_LEVEL` | Default **`INFO`** when JSON logging is enabled. |
| `ASTROCYTE_OTEL_ENABLED` | Set to **`1`** / **`true`** to load optional **`[otel]`** extras and export traces (set **`OTEL_EXPORTER_OTLP_ENDPOINT`**, **`OTEL_SERVICE_NAME`**, etc. per OpenTelemetry). |

If **`ASTROCYTE_CONFIG_PATH`** is **not** set, the process uses an empty `AstrocyteConfig` plus the env overrides above and applies **dev-style** defaults (PII off, access control off), matching the previous reference behavior. Install **`astrocyte-pgvector`** (`uv sync --extra pgvector`) before selecting `pgvector` as the vector store.

**Auth / identity (optional):** set **`ASTROCYTE_AUTH_MODE`** to `dev` (default), `api_key`, `jwt` / `jwt_hs256`, or **`jwt_oidc`** (RS256 + JWKS). For OIDC-style tokens:

| Variable | Meaning |
|----------|---------|
| `ASTROCYTE_AUTH_MODE` | Use `jwt_oidc` for RS256 + JWKS verification. |
| `ASTROCYTE_OIDC_JWKS_URL` | JWKS endpoint URL. |
| `ASTROCYTE_OIDC_ISSUER` | Expected `iss` claim. |
| `ASTROCYTE_OIDC_AUDIENCE` | Expected `aud` claim. |
| `ASTROCYTE_OIDC_ACTOR_TYPE` | Default actor type if `astrocyte_actor_type` is absent in the token (default `user`). |

Claims **`astrocyte_actor_type`**, **`tid`** / **`tenant_id`**, and optional **`astrocyte_principal`** map into **`AstrocyteContext`** / **`ActorIdentity`** (see ADR-002 in-repo).

## Identity

In **`dev`** mode, send optional header **`X-Astrocyte-Principal`** (for example `user:alice`) when `access_control` is enabled in config and grants are configured on the `Astrocyte` instance. **This header is not authenticated** in `dev` mode; use **`api_key`** or **JWT** modes for production (see [`docs/_end-user/production-grade-http-service.md`](../../docs/_end-user/production-grade-http-service.md)).

## HTTP API (summary)

| Method | Path | Body (JSON) |
|--------|------|-------------|
| `GET` | `/live` | - (process up; no DB check) |
| `GET` | `/health` | - (includes vector store / DB) |
| `POST` | `/v1/retain` | `content`, `bank_id`; optional `metadata`, `tags` |
| `POST` | `/v1/recall` | `query`; `bank_id` or `banks`; optional `max_results`, `max_tokens`, `tags` |
| `POST` | `/v1/reflect` | `query`, `bank_id`; optional `max_tokens`, `include_sources` |
| `POST` | `/v1/forget` | `bank_id`; optional `memory_ids`, `tags` |
| `POST` | `/v1/ingest/webhook/{source_id}` | Raw body + `Content-Type` — must match a **`sources:`** entry in config (HMAC or `auth: none` for demos) |
| `GET` | `/v1/admin/sources` | Lists configured ingest sources and best-effort health |
| `GET` | `/v1/admin/banks` | Bank ids from **`banks:`** in config |

OpenAPI docs: `/docs` when the HTTP service is running.

**Observability:** Every response includes **`X-Request-ID`** (or echoes the client’s **`X-Request-ID`**). One access line per request is logged (JSON when **`ASTROCYTE_LOG_FORMAT=json`**). Optional OpenTelemetry: **`uv sync --extra otel`**, **`ASTROCYTE_OTEL_ENABLED=1`**, and standard **`OTEL_*`** variables — see **`production-grade-http-service.md`** §3.6.

**Example configs** (Tier 1, MIP, webhook) are grouped under **[`examples/`](./examples/)** — each scenario has its **own subfolder** with an **`astrocyte.yaml`** (or MIP-only `mip.yaml`). Set **`ASTROCYTE_CONFIG_PATH`** to that file (absolute path, or run the gateway with cwd inside that folder).

**Tests:** from **`astrocyte-services-py/astrocyte-gateway/`**, run **`uv sync --extra dev --extra pgvector`** then **`uv run python -m pytest`** (use `python -m pytest` so the project venv is used). Integration against Postgres is in **`tests/test_integration_pgvector_http.py`** and runs in CI when **`DATABASE_URL`** is set and migrations have been applied (**`ASTROCYTE_GATEWAY_E2E_MIGRATED=1`** after `migrate.sh`).

## Docker

### Gateway + Postgres (pgvector)

From **`astrocyte-services-py/`** (or pass `-f` from the repo root):

```bash
cp .env.example .env   # optional: set secrets and ports
docker compose up --build
```

Defaults expose **API** on **8080** and **Postgres** on **5433**; override with **`ASTROCYTE_HTTP_PUBLISH_PORT`** and **`POSTGRES_PUBLISH_PORT`** in [`.env.example`](../.env.example). On the host, use `postgresql://USER:PASSWORD@127.0.0.1:POSTGRES_PUBLISH_PORT/DB` matching your `.env`. Inside Compose, `DATABASE_URL` points at the `postgres` service (see `.env.example`).

**Full deploy (one command):** from [`astrocyte-services-py/`](../), run **`./scripts/runbook-up.sh`** (see **[Runbook](../README.md#runbook)**).

### Gateway image only

From the **repository root** (`astrocyte/`):

```bash
docker build -f astrocyte-services-py/astrocyte-gateway/Dockerfile -t astrocyte-gateway .
docker run --rm -p 8080:8080 astrocyte-gateway
```

Then `GET http://localhost:8080/health`.

**GHCR (releases):** Pushing a version tag `v*` runs [`.github/workflows/publish-astrocyte-gateway-image.yml`](../../.github/workflows/publish-astrocyte-gateway-image.yml) — it re-runs the same library + gateway tests as CI, then builds and pushes **`ghcr.io/<owner>/<repo>/astrocyte-gateway:<tag>`** and **`:latest`**, and **attests** the image (SLSA provenance via GitHub). Pull with `docker pull ghcr.io/OWNER/REPO/astrocyte-gateway:v1.2.3` (replace `OWNER/REPO`; the image may default to **private** until you change package visibility under **Packages** in the org/repo). Branch protection: **[`BRANCH-PROTECTION.md`](./BRANCH-PROTECTION.md)**; release checklist: **[`RELEASE.md`](./RELEASE.md)**.

### Helm

A minimal chart lives at **[`../helm/astrocyte-gateway/`](../helm/astrocyte-gateway/)**. Build/push an image, set **`image.repository`** / **`image.tag`**, and inject **`DATABASE_URL`** (and optional **`ASTROCYTE_CONFIG_PATH`**) via **`env`** or **`extraEnvFrom`** (for example a Secret). With a ConfigMap volume for YAML, set **`configMapName`** and add **`ASTROCYTE_CONFIG_PATH=/config/astrocyte.yaml`** to **`env`**.
