# Production-grade HTTP service

This document describes how to operate Astrocyte **behind an HTTP (or similar) API** in production: security, durability, observability, and compliance. It applies whether you **embed** `Astrocyte` in your own service or use the optional **`astrocyte-rest`** reference HTTP service in this repository.

For the optional inbound edge and where a gateway sits relative to the core, see `architecture-framework.md` §3. For principals and banks, see `access-control.md`. For identity and external policy engines, see `identity-and-external-policy.md`. For outbound HTTP to LLMs and proxies, see `outbound-transport.md`.

**Checklists:** Items below use `- [ ]` so they render as **checkboxes** in GitHub, GitLab, VS Code, and many Markdown previews. Copy the doc or track items in your issue tracker.

---

## 1. Audience

**Implementing your own production service**

Use the **implementation checklist** (§3) as the primary guide. You own the process (FastAPI, gRPC, Lambda, etc.), TLS termination, identity, and scaling. The Astrocyte core remains a **library**; your service maps authenticated callers to `AstrocyteContext` and invokes `retain` / `recall` / `reflect` / `forget`.

**Using or hardening the reference REST service (`astrocyte-rest`)**

The repository includes [`astrocyte-services-py/astrocyte-rest/`](../astrocyte-services-py/astrocyte-rest/README.md) as an optional **REST** process built on **`astrocyte-py`**. §4 documents its purpose, layout, configuration, **local Compose / Makefile / troubleshooting** (§4.5), and how it maps to this checklist. §5 lists **repository-specific** follow-ups for that package.

---

## 2. Architecture reminder

An HTTP API is **not** part of the Astrocyte core contract. Typical production shape:

- **Edge:** API gateway or ingress (TLS, coarse rate limits, JWT or API-key validation).
- **Application:** Your service (or the reference `astrocyte-rest` package) constructs `Astrocyte`, attaches Tier 1 or Tier 2 providers, and passes an **opaque `principal`** on each call after **AuthN**.

The framework enforces **AuthZ** on memory banks when access control is configured. Do **not** treat unauthenticated client headers as proof of identity.

---

## 3. Implementation checklist

Track completion in your issue tracker or PRs as needed.

### 3.0 Scope and definition of done

- [ ] **Product decision:** Confirm target environments (Kubernetes, VM, single-tenant SaaS, air-gapped) and **SLOs** (availability, p95 latency, error budget).
- [ ] **Support boundary:** Document what is supported (API versions, auth modes, backends) versus experimental.
- [ ] **Threat model:** Document trust zones (internet, VPC, mesh), who may call the API, and what data may appear in logs.

### 3.1 Memory backends and durability (not in-memory)

- [ ] **Tier 1:** Use production **VectorStore** adapters (and optional GraphStore / DocumentStore) per `provider-spi.md`. Avoid in-process-only stores for durable memory. This repository includes an optional **[`astrocyte-pgvector`](../astrocyte-services-py/astrocyte-pgvector/README.md)** adapter (PostgreSQL + pgvector; **[`docker-compose.yml`](../astrocyte-services-py/docker-compose.yml)** under **`astrocyte-services-py/`** runs API + Postgres); wire it via config entry point **`pgvector`** and the same **`astrocyte_rest/wiring.py`** resolution path as other Tier 1 stores. **Compose networking:** the API container must use a DSN with the Postgres **service hostname** (`postgres`), not a host-only URL such as `127.0.0.1:5433`. The repo’s Compose file sets **`DATABASE_URL`** from **`ASTROCYTES_REST_DATABASE_URL`** or builds `...@postgres:5432/...` from **`POSTGRES_*`**; use **`MIGRATE_DATABASE_URL`** (or a one-off `DATABASE_URL` only for the shell) for **host-side** `migrate.sh`, not for the in-cluster API—see [`astrocyte-services-py/.env.example`](../astrocyte-services-py/.env.example).
- [ ] **Tier 2 (optional):** If using a memory engine provider, wire **EngineProvider** and validate **capability negotiation** (`reflect`, `forget`, etc.).
- [ ] **LLM / embeddings:** Use real **LLMProvider** (and embedding path) appropriate to latency and cost.
- [ ] **Configuration:** Load provider entry points from **config** (YAML/env) with validation; fail fast on missing required settings in prod.
- [ ] **Data durability:** Define **backup, restore, and RPO/RTO** for each store; test restore drills.
- [ ] **Migrations:** If stores require schema migrations, own a **migration** process (job or init container) and versioning. For **`astrocyte-pgvector`**, use the shipped **SQL** files and **`psql`** runner ([`migrate.sh`](../astrocyte-services-py/astrocyte-pgvector/scripts/migrate.sh)); set **`bootstrap_schema: false`** in `vector_store_config` after applying migrations.

### 3.2 Authentication (AuthN) - do not trust client-supplied principals

- [ ] **Remove or gate dev behavior:** A header such as `X-Astrocyte-Principal` must **not** be the only identity in production unless behind verified **mTLS** or a **trusted gateway** that strips spoofed headers.
- [ ] **Choose and implement one primary mode:**
  - [ ] **JWT** (OIDC): validate issuer, audience, signature, `exp`; map claims to `AstrocyteContext.principal`.
  - [ ] **API keys:** hashed keys in store, rotation, per-key scopes.
  - [ ] **mTLS:** client certificates mapped to principals.
- [ ] **Service-to-service:** Use workload identity (SPIFFE, IAM) where applicable.
- [ ] **Anonymous / public:** Explicitly forbid or allow only for specific routes; document risk.

### 3.3 Authorization (AuthZ) and tenancy

- [ ] **Enable framework access control:** `access_control` on with explicit policy (not `open` by default in prod).
- [ ] **Grants:** Load **AccessGrant** (or equivalent) from config, database, or **external PDP** (OPA, Cerbos, Casbin) per `access-control.md`.
- [ ] **Bank isolation:** Enforce **bank_id** scoping; prevent cross-tenant **IDOR** (validate bank belongs to tenant/principal).
- [ ] **Admin paths:** Separate privileged operations (if any) with stricter checks.
- [ ] **Optional external PDP:** Integrate **AccessPolicyProvider** when enterprise policy engines are required (`identity-and-external-policy.md`).

### 3.4 Network, TLS, and edge

- [ ] **Memory API exfiltration:** Treat Astrocyte like any **sensitive backend**: bind **AuthN** at the **Backend for Frontend (BFF)** (do not let sandboxed clients freely choose a production **principal** or bank); combine with **network egress** policy for agent workloads so **recall** cannot be relayed to the public internet—see `sandbox-awareness-and-exfiltration.md` and [Let’s discuss sandbox isolation](https://www.shayon.dev/post/2026/52/lets-discuss-sandbox-isolation/).
- [ ] **TLS:** Terminate TLS at **ingress/gateway** or in-process; **HSTS** and modern cipher policy where browsers are involved.
- [ ] **Private networking:** Prefer private subnets / mesh; no public exposure without WAF/rate limits as needed.
- [ ] **Request limits:** Max body size, header size, URL length; **timeouts** on client and server.
- [ ] **CORS:** If browser clients exist, **restrict** origins; avoid `*` in production.
- [ ] **Rate limiting:** Edge (API gateway) + align with Astrocyte **homeostasis** / quotas in config (`policy-layer.md`).

### 3.5 API design and contract

- [ ] **Versioning:** Keep `/v1` (or header versioning) with a **compatibility policy**; deprecate with lead time.
- [ ] **OpenAPI:** Publish stable **OpenAPI**; generate clients if useful; CI diff on API changes.
- [ ] **Pagination / limits:** Cap `max_results`, `max_tokens`, and content size consistently; return **truncation** flags where applicable.
- [ ] **Idempotency:** For `retain`, define **idempotency keys** or dedup strategy for retries (align with framework behavior).
- [ ] **Error shape:** Stable JSON error body (`code`, `message`, `request_id`); no stack traces to clients in prod.
- [ ] **Health endpoints:**
  - [ ] **Liveness:** process up without hitting backends (reference **`astrocyte-rest`:** `GET /live` or `GET /health/live`).
  - [ ] **Readiness:** dependencies reachable (reference: `GET /health` runs **`Astrocyte.health()`** → vector store check; with **pgvector** that implies PostgreSQL connectivity). Use a **bounded** server-side timeout for deep checks so load balancers do not hang; expect **503** if dependencies fail or exceed the timeout.

### 3.6 Observability

- [ ] **Structured logs:** JSON logs with **request_id**, **principal** (hashed or opaque id if sensitive), **bank_id**, **route**, **latency**, **status**; **PII redaction** policy.
- [ ] **Metrics:** RED (rate, errors, duration) per route; saturation (CPU, memory, queue depth); dependency health.
- [ ] **Tracing:** **OpenTelemetry** traces across HTTP and outbound calls (LLM, stores); optional `astrocyte` OTel extras (`astrocyte-py` optional dependencies).
- [ ] **Dashboards and alerts:** SLO-based alerts (burn rate) on error rate and latency.

### 3.7 Reliability and resilience

- [ ] **Graceful shutdown:** Honor SIGTERM; drain in-flight requests within a **bounded** time (uvicorn/gunicorn settings).
- [ ] **Timeouts:** Per-route and per-outbound-call timeouts; avoid unbounded `await` chains.
- [ ] **Retries:** Safe retries for **read** paths only unless idempotent writes are guaranteed.
- [ ] **Circuit breaking:** Align gateway and framework **escalation** / circuit breaker settings with dependency behavior.
- [ ] **Overload:** Backpressure (503 + `Retry-After`) when saturated; consider queue or shed load policy.

### 3.8 Security hardening (application and supply chain)

- [ ] **Dependencies:** Pin versions; **SBOM**; automated **CVE** scanning on images and lockfiles.
- [ ] **Secrets:** No secrets committed in repos; use **secret manager** / K8s secrets / IAM roles.
- [ ] **Process:** Run as **non-root** in containers; **read-only** root filesystem where possible; **capability** drop.
- [ ] **Headers:** Strip or validate **forwarded** headers; prevent header injection in logs.
- [ ] **Input validation:** Reject unexpected JSON fields if strict mode is desired; validate `bank_id` format and length.

### 3.9 Container image and runtime

- [ ] **Base image:** Minimal, pinned digest; regular rebuilds for CVE patches.
- [ ] **Multi-stage build:** Optional, to reduce attack surface and size.
- [ ] **Image signing:** Cosign / policy in cluster to verify signatures.
- [ ] **Resource limits:** CPU/memory **requests and limits**; avoid OOM under load.
- [ ] **Probes:** Kubernetes **liveness** and **readiness** using the endpoints from §3.5.

### 3.10 Data governance and compliance

- [ ] **Classification:** Tag banks or payloads per **data class** if required (`data-governance.md`).
- [ ] **Retention and legal hold:** Align with `memory-lifecycle.md`; block deletes when under hold.
- [ ] **Audit log:** Immutable audit stream for retain/forget/admin actions (who, when, bank, outcome).
- [ ] **Residency:** Ensure stores and LLM regions meet **data residency** requirements.
- [ ] **PII:** Configure **barriers** / PII policy deliberately; document **false positive** handling (`policy-layer.md`).

### 3.11 Performance and capacity

- [ ] **Load testing:** Establish **p95/p99** under target QPS and payload sizes; test **cold start** and **warm** pools.
- [ ] **Connection pools:** HTTP clients to LLM and DBs use bounded pools and sane keep-alive.
- [ ] **Horizontal scaling:** Stateless replicas; **sticky sessions** only if you introduce local-only state (avoid).
- [ ] **Cost controls:** Token and spend caps aligned with **homeostasis** and product limits.

### 3.12 Testing and quality gates

- [ ] **Contract tests:** HTTP API vs OpenAPI; backward compatibility tests on `/v1`.
- [ ] **Integration tests:** Real (or containerized) dependencies in CI for critical paths.
- [ ] **Security tests:** AuthZ tests (tenant A cannot read tenant B), fuzzing on inputs, OWASP API checks.
- [ ] **Chaos / failure injection:** Dependency failures (DB down, LLM timeout) produce expected errors and no data corruption.

### 3.13 Operations and lifecycle

- [ ] **Runbooks:** Incident response, **key rotation**, scaling, **failover**, and **rollback** of releases.
- [ ] **Configuration rollout:** Safe rollout of config changes (feature flags or staged deploys).
- [ ] **Backup and DR:** Documented restore tests; RPO/RTO validated annually or on change.
- [ ] **On-call:** Alert routing and escalation paths tied to SLOs.

---

## 4. Reference REST service (`astrocyte-rest`)

This repository ships an optional **`astrocyte-rest`** HTTP service that embeds **`astrocyte-py`**. It is a **convenience** for development and as a **starting point** for a custom deployment; it is **not** production-ready by default.

### 4.1 Purpose

- Demonstrate mapping HTTP JSON bodies to **`Astrocyte`** calls (`retain`, `recall`, `reflect`, `forget`) and optional **`X-Astrocyte-Principal`**.
- Provide a **Dockerfile** so the stack can be run locally or in a sandbox without writing a host app.

### 4.2 Current behavior (non-production defaults)

- **Tier 1 pipeline** resolved from config: defaults are **`in_memory`** vector store and **`mock`** LLM (entry points on `astrocyte-py`). Optional YAML / env can select other registered providers or **`module:Class`** paths (for example **`pgvector`** after installing [`astrocyte-pgvector`](../astrocyte-services-py/astrocyte-pgvector/README.md)). Data is **not** durable when using the built-in in-memory stack.
- **Access control** defaults to **off** when no config file is loaded; when you enable **`access_control`** in YAML, **`access_grants`** and **`banks.*.access`** are loaded from config and applied via **`set_access_grants`** (see §5). You still need a **deliberate** prod policy—do not rely on defaults.
- **Identity:** **`ASTROCYTES_AUTH_MODE`** selects **`dev`** (trusts **`X-Astrocyte-Principal`** only—use only behind a trusted gateway), **`api_key`**, or **`jwt_hs256`** (Bearer JWT, `sub` → principal). This is a **starting point** for §3.2, not a full IdP integration (no OIDC discovery, JWKS, or per-key store yet).

Treat this as a **reference implementation** of the HTTP mapping only, not as a hardened product.

### 4.3 Layout and entrypoints

| Item | Location |
|------|----------|
| Package | [`astrocyte-services-py/astrocyte-rest/astrocyte_rest/`](../astrocyte-services-py/astrocyte-rest/astrocyte_rest/) |
| FastAPI app factory | `app.py` - `create_app()` |
| Brain wiring | `brain.py` - `build_reference_astrocyte()` |
| CLI | `astrocyte-rest` (see [`pyproject.toml`](../astrocyte-services-py/astrocyte-rest/pyproject.toml)) |
| Container | [`Dockerfile`](../astrocyte-services-py/astrocyte-rest/Dockerfile) (build from **repository root**; see [`README`](../astrocyte-services-py/astrocyte-rest/README.md)) |
| Compose (API + Postgres) | [`docker-compose.yml`](../astrocyte-services-py/docker-compose.yml) |
| Runbook overlay (migrations + `bootstrap_schema: false`) | [`docker-compose.runbook.yml`](../astrocyte-services-py/docker-compose.runbook.yml), [`config.runbook.example.yaml`](../astrocyte-services-py/config.runbook.example.yaml) |
| One-shot local deploy | [`scripts/runbook-up.sh`](../astrocyte-services-py/scripts/runbook-up.sh) |
| Common commands | [`Makefile`](../astrocyte-services-py/Makefile) (`make runbook`, `make up`, `make health`, …) |

### 4.4 Configuration surface (today)

| Variable | Role |
|----------|------|
| `ASTROCYTES_HOST` / `ASTROCYTES_PORT` | Bind address and port |
| `ASTROCYTES_CONFIG_PATH` | Optional YAML loaded via `load_config` for policy/homeostasis; **in-memory Tier 1 pipeline is still attached** in the reference implementation |
| `DATABASE_URL` / `ASTROCYTES_PG_DSN` | **pgvector:** PostgreSQL URI. In **Docker Compose**, the service sets **`DATABASE_URL`** for the API from **`ASTROCYTES_REST_DATABASE_URL`** or a built-in `...@postgres:5432/...` DSN (see [`docker-compose.yml`](../astrocyte-services-py/docker-compose.yml)). Do **not** point the API container at a host-only URL (`127.0.0.1:published_port`) via a generic **`DATABASE_URL`** in `.env`. |
| `MIGRATE_DATABASE_URL` | Optional **host-side** DSN for [`migrate.sh`](../astrocyte-services-py/astrocyte-pgvector/scripts/migrate.sh) / `runbook-up.sh` (typically `127.0.0.1` + `POSTGRES_PUBLISH_PORT`). |

See [`astrocyte-services-py/astrocyte-rest/README.md`](../astrocyte-services-py/astrocyte-rest/README.md) for run instructions, HTTP route summary (including **`GET /live`**, **`GET /health/live`**, **`GET /health`**), and Docker commands.

### 4.5 Local Compose, Makefile, and troubleshooting

- **Runbook (recommended for durable schema):** From [`astrocyte-services-py/`](../astrocyte-services-py/), `./scripts/runbook-up.sh` or **`make runbook`** starts Postgres, runs SQL migrations on the host port, then **`docker compose`** with the runbook overlay. Details: [`astrocyte-services-py/README.md`](../astrocyte-services-py/README.md) (Runbook, Verify, **Debugging**).
- **Quick stack:** **`make up`** or `docker compose up --build` uses in-container bootstrap DDL unless you add the runbook file; for HNSW / migration-owned DDL, use the runbook path.
- **Health checks:** **`GET /live`** (or **`GET /health/live`**) confirms the process only; **`GET /health`** checks **`Astrocyte.health()`** (with **pgvector**, a real DB round-trip). If `/live` succeeds and `/health` fails or times out, inspect **`docker compose logs astrocyte-rest`**, **`printenv DATABASE_URL`** inside the API container, and the **Debugging** section of [`astrocyte-services-py/README.md`](../astrocyte-services-py/README.md).
- **Implementation note:** The **`astrocyte-pgvector`** adapter uses **psycopg 3** async pools with **`register_vector_async`** and explicit transaction boundaries in pool `configure` callbacks (`provider-spi.md` §7.1).

### 4.6 Path to production for `astrocyte-rest`

Align **`astrocyte-rest`** with §3: real backends, verified AuthN, AuthZ with grants, health/readiness, observability, hardened image, and the items in §5. Until then, run it only in **trusted** dev or demo environments.

---

## 5. Repository-specific follow-ups (`astrocyte-rest`)

- [x] **Brain wiring:** `build_astrocyte()` in `astrocyte_rest/brain.py` loads `AstrocyteConfig` and calls `build_tier1_pipeline()` in `astrocyte_rest/wiring.py`, which resolves **`vector_store`**, **`llm_provider`**, and optional graph/document stores via **`astrocyte._discovery.resolve_provider`** (entry points or `package.module:Class`). Built-in names ship in **`astrocyte-py`** `pyproject.toml` (`in_memory`, `mock`). A PostgreSQL **`pgvector`** adapter lives in **[`astrocyte-pgvector`](../astrocyte-services-py/astrocyte-pgvector/README.md)** (optional **`pgvector`** extra on `astrocyte-rest`).
- [x] **Health routes:** **`GET /live`** and **`GET /health/live`** (liveness); **`GET /health`** (readiness / dependency check with bounded timeout). Documented in [`astrocyte-rest/README.md`](../astrocyte-services-py/astrocyte-rest/README.md).
- [x] **pgvector + psycopg async:** Pool **`configure`** uses **`register_vector_async`**, commits after `configure`, and registers vector types only when the **`vector`** extension is present (see `provider-spi.md` §7.1).
- [x] **Grants from config:** When **`access_control.enabled`**, grants are loaded from YAML (**`access_grants`** and **`banks.*.access`**) via **`access_grants_for_astrocyte()`** in **`astrocyte-py`** and **`set_access_grants()`** in **`astrocyte_rest/brain.py`** — see `identity-and-external-policy.md` §8. **Not done here:** grants from a **database** or **external PDP** (still your integration or future work).
- [ ] **Profiles:** Keep PII `disabled` and `access_control.enabled = False` only for explicit **dev** profile; document prod profile requirements.
- [ ] **MCP / CLI:** If `astrocyte-mcp` is shipped, align its security model with the HTTP service (same AuthN story). See `mcp-server.md`.

---

## 6. Sign-off (optional)

| Area | Owner | Date | Notes |
|------|--------|------|--------|
| Security review | | | |
| SLO agreed | | | |
| Launch readiness | | | |
