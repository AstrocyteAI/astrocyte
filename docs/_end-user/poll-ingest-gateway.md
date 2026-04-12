# Poll ingest with the standalone gateway

Use **`type: poll`** / **`api_poll`** in **`astrocyte.yaml`** so the gateway starts long-running ingest sources (for example **GitHub Issues** via **`astrocyte-ingestion-github`**) together with **`astrocyte-gateway-py`**. Works with **`provider_tier: storage`** (built-in pipeline) or **`engine`**; defaults in examples often use **`engine`** with **`in_memory`** / **`mock`** for local demos.

## 1. Install extras

The core wheel does not bundle poll drivers. Install one of:

```bash
pip install 'astrocyte[poll]'
# or
pip install astrocyte-ingestion-github
```

Use the same environment as **`astrocyte-gateway-py`** (same venv or image).

## 2. Configure `sources:`

Example: poll **`owner/repo`** into bank **`github_issues`** every 120 seconds:

```yaml
provider_tier: engine
vector_store: in_memory
llm_provider: mock

sources:
  gh_issues:
    type: poll
    driver: github
    path: myorg/myrepo
    interval_seconds: 120
    target_bank: github_issues
    auth:
      token: ${GITHUB_TOKEN}
```

- **`interval_seconds`**: **`validate_astrocyte_config`** requires **≥ 60** for poll sources. Use **60+** in production for GitHub: authenticated REST is capped (e.g. **5,000 requests/hour** per token for typical use), and shorter intervals burn budget fast on active repos.
- **`auth.token`**: classic PAT or fine-grained token with **`issues`** read access to the repo.

## 3. Run the gateway

```bash
export ASTROCYTE_CONFIG_PATH=/path/to/astrocyte.yaml
astrocyte-gateway-py
```

On startup the **`IngestSupervisor`** runs **`start()`** for each configured source; GitHub poll opens a background loop.

## 4. Align MCP defaults (optional)

If you also run **`astrocyte-mcp`** with the same config, set **`mcp.default_bank_id`** to the bank poll writes to so MCP tools default to the same memory without passing **`bank_id`** every time:

```yaml
mcp:
  default_bank_id: github_issues
```

## 5. Health and observability

- **`GET /health/ingest`** — JSON snapshot of ingest source health (no admin token). Use for probes that should fail when poll/stream sources are unhealthy.
- **`GET /v1/admin/sources`** — same **`sources`** list when **`ASTROCYTE_ADMIN_TOKEN`** is set (see gateway README).
- **Structured logs**: set **`ASTROCYTE_LOG_FORMAT=json`** (gateway). Ingest emits JSON lines for supervisor lifecycle, GitHub rate-limit warnings, stream transport failures, etc. (see **`astrocyte.ingest.logutil`**).

## Further reading

- Design: **[`built-in-pipeline.md`](../_design/built-in-pipeline.md)** (poll ingest section).
- Adapter: **[`adapters-ingestion-py/astrocyte-ingestion-github/README.md`](../../adapters-ingestion-py/astrocyte-ingestion-github/README.md)**.
