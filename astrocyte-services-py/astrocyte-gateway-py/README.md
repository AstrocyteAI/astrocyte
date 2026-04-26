# Astrocyte gateway (Python)

Optional **standalone HTTP** service for [`astrocyte-py`](../../astrocyte-py/): one process exposes `retain` / `recall` / `reflect` / `forget` over REST. **Behavior is not hard-coded in Python** — you drive almost everything through **`astrocyte.yaml`** (`AstrocyteConfig`: providers, policy, `recall_authority`, banks, access grants, …) and optionally **`mip.yaml`** via **`mip_config_path`** in that same YAML for Memory Intent Protocol routing.

**Defaults:** Tier 1 providers resolve from config (built-in **`in_memory`** stores and **`mock`** LLM unless you override). Data is **not** durable with in-memory backends. The gateway is a **convenience and starting point** for deployments; treat production hardening as mandatory — see below.

**Production and operations:** Checklist and ops context are in **[`docs/_end-user/production-grade-http-service.md`](../../docs/_end-user/production-grade-http-service.md)** (especially §3 and §4 gateway section).

### Quick start (official image from GHCR)

After a **`v*`** tag, CI publishes **`ghcr.io/<github-owner>/<repo>/astrocyte-gateway-py:<tag>`** and **`:latest`** (see **[`RELEASE.md`](./RELEASE.md)**). Example:

```bash
docker pull ghcr.io/astrocyteai/astrocyte/astrocyte-gateway-py:latest
docker run --rm -p 8080:8080 \
  -e ASTROCYTE_HOST=0.0.0.0 \
  -e ASTROCYTE_CONFIG_PATH=/config/astrocyte.yaml \
  -v /abs/path/to/examples/tier1-minimal/astrocyte.yaml:/config/astrocyte.yaml:ro \
  ghcr.io/astrocyteai/astrocyte/astrocyte-gateway-py:latest
```

Replace **`astrocyteai/astrocyte`** with your GitHub **`owner/repo`**. For **pgvector**, add **`DATABASE_URL`** (and use a config that sets `vector_store: pgvector`). Copy **[`examples/`](./examples/)** as a starting point.

## Run locally

Install `astrocyte` first, then this package:

```bash
cd /path/to/astrocyte/astrocyte-py && pip install -e .
cd /path/to/astrocyte/astrocyte-services-py/astrocyte-gateway-py && pip install -e .
ASTROCYTE_HOST=0.0.0.0 ASTROCYTE_PORT=8080 astrocyte-gateway-py
```

Or:

```bash
uv run --directory astrocyte-services-py/astrocyte-gateway-py astrocyte-gateway-py
```

**uv:** `pyproject.toml` pins **`[tool.uv.sources]`** so `astrocyte` resolves from **`../../astrocyte-py`** (editable). Run `uv sync` from **`astrocyte-services-py/astrocyte-gateway-py/`**. Plain **`pip`** users should `pip install -e ../../astrocyte-py` first, then install this package.

### Gateway overhead benchmark (local)

Measures **in-process** ASGI overhead for `POST /v1/recall` vs calling `brain.recall` directly on the **same** `Astrocyte` instance (`httpx.ASGITransport`, no TCP). Uses default in-memory + mock providers unless you set `ASTROCYTE_CONFIG_PATH`. Requires **`httpx`** (`uv sync --extra dev`).

```bash
cd astrocyte-services-py/astrocyte-gateway-py
uv sync --extra dev
uv run python scripts/bench_gateway_overhead.py
# Machine-readable output:
uv run python scripts/bench_gateway_overhead.py --json
# Fewer iterations (faster smoke):
uv run python scripts/bench_gateway_overhead.py --warmup 20 --iterations 200

# Optional SLO gate (exit code 2 if p99 overhead exceeds 10 ms — tune for your environment):
uv run python scripts/bench_gateway_overhead.py --warmup 80 --iterations 1500 --max-overhead-p99-ms 10

# Real TCP + uvicorn (loopback, uvicorn in a background thread — closer to production than ASGITransport):
uv run python scripts/bench_gateway_overhead.py --tcp --warmup 20 --iterations 200
```

**Fully external process (optional):** start the gateway in one shell, then hit it with any HTTP client (no paired “direct recall” subtraction in-process):

Run **`uv sync`** once from this package directory so **`astrocyte`** resolves from **`../../astrocyte-py`**.

Use **`uv run python -m uvicorn ...`** (recommended). On some machines **`uv run uvicorn`** still executes a **`uvicorn`** script from **pyenv/global PATH** (`~/.pyenv/.../bin/uvicorn`) instead of the project `.venv`, which triggers **`ModuleNotFoundError: No module named 'astrocyte'`**. Invoking **`python -m uvicorn`** forces the interpreter from **`uv run`**’s environment.

```bash
# Terminal A — cwd must be this package (editable astrocyte via uv.sources)
cd /path/to/astrocyte/astrocyte-services-py/astrocyte-gateway-py
uv sync
ASTROCYTE_AUTH_MODE=dev ASTROCYTE_PORT=18080 \
  uv run python -m uvicorn astrocyte_gateway.app:create_app --factory --host 127.0.0.1 --port 18080 --access-log --log-level warning

# Terminal B (example: curl)
curl -sS -X POST http://127.0.0.1:18080/v1/recall -H 'Content-Type: application/json' \
  -d '{"query":"bench","bank_id":"b1","max_results":5}' | head -c 200
```

That measures **end-to-end HTTP latency** only (includes another process and whatever core recall costs); use it for load tests or to mirror deployment topology.

On GitHub: **Actions → Benchmark gateway overhead → Run workflow** (manual dispatch; prints JSON to the job summary).

**Concurrent HTTP smoke (optional):** with uvicorn running (local or CI), **`scripts/smoke_gateway_load.py`** sends many parallel **`POST /v1/recall`** requests and exits **non-zero** if any response is not **200**. GitHub Actions: **Actions → Smoke test gateway HTTP → Run workflow** (starts the gateway in the job, then runs the script; checks success rate only, not latency SLOs).

**PostgreSQL reference stack (pgvector + Apache AGE):** Install the optional adapters (`uv sync --extra pgvector --extra age`) and run Postgres. The fastest path is **Docker Compose** at **[`../docker-compose.yml`](../docker-compose.yml)** (repo **`astrocyte-services-py/`**), which starts the combined Postgres image + this service together. For Postgres only on the host, see [`astrocyte-pgvector`](../../adapters-storage-py/astrocyte-pgvector/README.md) and `adapters-storage-py/astrocyte-age/`.

Then set `vector_store: pgvector` and optionally `graph_store: age` in YAML, or use `ASTROCYTE_VECTOR_STORE=pgvector` / `ASTROCYTE_GRAPH_STORE=age`, and pass a DSN via provider config or `DATABASE_URL` / `ASTROCYTE_PG_DSN` / `ASTROCYTE_AGE_DSN`.

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
| `ASTROCYTE_WIKI_STORE` | Optional wiki store name. Use `in_memory` for local M8 compile demos; production adapters should persist pages durably. |
| `DATABASE_URL` / `ASTROCYTE_PG_DSN` | When using **`pgvector`**, connection URI for PostgreSQL (see [`astrocyte-pgvector`](../../adapters-storage-py/astrocyte-pgvector/README.md)); can be omitted if `dsn` is set in YAML `vector_store_config`. |
| `ASTROCYTE_MAX_REQUEST_BODY_BYTES` | If set to a positive integer, reject requests whose **`Content-Length`** exceeds it (**413**). Unset = no limit (dev default). |
| `ASTROCYTE_CORS_ORIGINS` | Comma-separated allowed origins for **browser** `fetch` (e.g. `https://app.example.com`). Unset = CORS middleware not added (same-origin / server-to-server only). |
| `ASTROCYTE_RATE_LIMIT_PER_SECOND` | Optional **positive integer**: max requests per **1 s** rolling window **per client** (client = first **`X-Forwarded-For`** hop, else TCP peer). **`/live`**, **`/health*`** are exempt. Returns **429** with **`Retry-After: 1`**. Prefer coarse limits at Kong/APISIX/Azure APIM when exposed to the internet; use this as a backstop. Unset = no in-process limit. |
| `ASTROCYTE_ADMIN_TOKEN` | If set, **`GET /v1/admin/*`** requires header **`X-Admin-Token`** with the same value (use behind TLS; rotate like any secret). Unset = admin routes behave like other API routes (auth mode only). |
| `ASTROCYTE_LOG_FORMAT` | Set to **`json`** for JSON lines on loggers `astrocyte_gateway` / `astrocyte_gateway.access` (aligns with §3.6 observability in production docs). Ingest packages also emit structured lines (**`astrocyte.ingest.logutil`**: supervisor lifecycle, GitHub rate limits, stream errors) when this is set. |
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
| `GET` | `/health/ingest` | - (ingest sources only: poll/stream/webhook health snapshot; **`status`** **`ok`** / **`degraded`**) |
| `POST` | `/v1/retain` | `content`, `bank_id`; optional `metadata`, `tags` |
| `POST` | `/v1/recall` | `query`; `bank_id` or `banks`; optional `max_results`, `max_tokens`, `tags` |
| `POST` | `/v1/reflect` | `query`, `bank_id`; optional `max_tokens`, `include_sources` |
| `POST` | `/v1/forget` | `bank_id`; optional `memory_ids`, `tags` |
| `POST` | `/v1/compile` | `bank_id`; optional `scope` (requires `wiki_store`) |
| `POST` | `/v1/audit` | `scope`, `bank_id`; optional `max_memories`, `max_tokens`, `tags` |
| `POST` | `/v1/history` | `query`, `bank_id`, `as_of`; optional `max_results`, `max_tokens`, `tags` |
| `POST` | `/v1/graph/search` | `query`, `bank_id`; optional `limit` (requires `graph_store`) |
| `POST` | `/v1/graph/neighbors` | `entity_ids`, `bank_id`; optional `max_depth`, `limit` |
| `POST` | `/v1/ingest/webhook/{source_id}` | Raw body + `Content-Type` — must match a **`sources:`** entry in config (HMAC or `auth: none` for demos) |
| `GET` | `/v1/admin/sources` | Lists configured ingest sources and best-effort health |
| `GET` | `/v1/admin/banks` | Bank ids from **`banks:`** in config |

OpenAPI docs: `/docs` when the HTTP service is running.

**Observability:** Every response includes **`X-Request-ID`** (or echoes the client’s **`X-Request-ID`**). One access line per request is logged (JSON when **`ASTROCYTE_LOG_FORMAT=json`**). Optional OpenTelemetry: **`uv sync --extra otel`**, **`ASTROCYTE_OTEL_ENABLED=1`**, and standard **`OTEL_*`** variables — see **`production-grade-http-service.md`** §3.6.

**Example configs** (Tier 1, MIP, webhook) are grouped under **[`examples/`](./examples/)** — each scenario has its **own subfolder** with an **`astrocyte.yaml`** (or MIP-only `mip.yaml`). Set **`ASTROCYTE_CONFIG_PATH`** to that file (absolute path, or run the gateway with cwd inside that folder).

**Poll ingest (GitHub Issues):** install **`astrocyte[poll]`** (or **`astrocyte-ingestion-github`**) and follow **[`docs/_end-user/poll-ingest-gateway.md`](../../docs/_end-user/poll-ingest-gateway.md)**.

**Tests:** from **`astrocyte-services-py/astrocyte-gateway-py/`**, run **`uv sync --extra dev --extra pgvector`** then **`uv run python -m pytest`** (use `python -m pytest` so the project venv is used). Integration against Postgres is in **`tests/test_integration_pgvector_http.py`** and runs in CI when **`DATABASE_URL`** is set and migrations have been applied (**`ASTROCYTE_GATEWAY_E2E_MIGRATED=1`** after `migrate.sh`).

## Docker

### Gateway + Postgres (pgvector + Apache AGE)

From **`astrocyte-services-py/`** (or pass `-f` from the repo root):

```bash
cp .env.example .env   # optional: set secrets and ports
docker compose up --build
```

Defaults expose **API** on **8080** and **Postgres** on **5433**; override with **`ASTROCYTE_HTTP_PUBLISH_PORT`** and **`POSTGRES_PUBLISH_PORT`** in [`.env.example`](../.env.example). On the host, use `postgresql://USER:PASSWORD@127.0.0.1:POSTGRES_PUBLISH_PORT/DB` matching your `.env`. Inside Compose, `DATABASE_URL` points at the `postgres` service (see `.env.example`).

**Full deploy (one command):** from [`astrocyte-services-py/`](../), run **`./scripts/runbook-up.sh`** (see **[Runbook](../README.md#runbook)**).

### Gateway image only

From the **repository root** (`astrocyte/`):

**Local / CI-from-source** (`Dockerfile` — vendors `astrocyte-py` + `astrocyte-pgvector` from this checkout):

```bash
docker build -f astrocyte-services-py/astrocyte-gateway-py/Dockerfile -t astrocyte-gateway-py .
docker run --rm -p 8080:8080 astrocyte-gateway-py
```

**Release / PyPI-pinned** (`Dockerfile.release` — `pip install astrocyte==X astrocyte-pgvector==X` from PyPI, then this package from the tree; use the same **PEP 440** version as your git tag without `v`):

```bash
docker build -f astrocyte-services-py/astrocyte-gateway-py/Dockerfile.release \
  --build-arg ASTROCYTE_VERSION=0.8.0 \
  --build-arg SETUPTOOLS_SCM_PRETEND_VERSION=0.8.0 \
  -t astrocyte-gateway-py:0.8.0 .
```

Then `GET http://localhost:8080/health`.

**GHCR (releases):** Pushing a version tag `v*` runs [`.github/workflows/release.yml`](../../.github/workflows/release.yml), which publishes **`astrocyte`** → **`astrocyte-pgvector`** to PyPI in order, then runs [`.github/workflows/publish-astrocyte-gateway-py-image.yml`](../../.github/workflows/publish-astrocyte-gateway-py-image.yml) to build with **`Dockerfile.release`** and push **`ghcr.io/<owner>/<repo>/astrocyte-gateway-py:<tag>`** and **`:latest`**, with **attestations**. Pull with `docker pull ghcr.io/OWNER/REPO/astrocyte-gateway-py:v1.2.3`. Branch protection: **[`BRANCH-PROTECTION.md`](./BRANCH-PROTECTION.md)**; release checklist: **[`RELEASE.md`](./RELEASE.md)**.

### Helm

A minimal chart lives at **[`../helm/astrocyte-gateway-py/`](../helm/astrocyte-gateway-py/)**. Build/push an image, set **`image.repository`** / **`image.tag`**, and inject **`DATABASE_URL`** (and optional **`ASTROCYTE_CONFIG_PATH`**) via **`env`** or **`extraEnvFrom`** (for example a Secret). With a ConfigMap volume for YAML, set **`configMapName`** and add **`ASTROCYTE_CONFIG_PATH=/config/astrocyte.yaml`** to **`env`**.
