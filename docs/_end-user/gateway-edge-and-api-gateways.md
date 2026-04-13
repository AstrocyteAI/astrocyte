# Gateway edge: API management, reverse proxies, and transports

This page complements **[Production-grade HTTP service](./production-grade-http-service.md)** with **v0.8.x** guidance for:

- Putting **Kong**, **Envoy**, **NGINX**, **APISIX**, **Azure API Management**, or similar **in front of** `astrocyte-gateway-py`
- Choosing **edge vs in-process** controls (**CORS**, **body size**, **rate limits**)
- Planning **additional ingest transports** (streams, poll drivers) on the connector track

---

## 1. Reverse proxy and API gateways (recommended for production)

Typical placement:

```text
Internet / VPC → API gateway or ingress (TLS, WAF, coarse rate limits)
             → optional service mesh / internal LB
             → astrocyte-gateway-py (FastAPI)
             → Astrocyte core + Tier 1 stores
```

**TLS and certificates** terminate at the edge or ingress, not necessarily inside the gateway container. Pass **HTTPS** to clients only; the hop from proxy to gateway is often **HTTP** on a private network or **mTLS** where policy requires it.

**Trust and headers**

- Configure the proxy to set **`X-Forwarded-For`**, **`X-Forwarded-Proto`**, and **`X-Forwarded-Host`** when the gateway or operators need correct client IP or URLs (OpenAPI links, redirects). The optional **per-client rate limit** in the gateway uses the **first** **`X-Forwarded-For`** hop when present.
- Do **not** forward unverified **`X-Astrocyte-Principal`** from browsers; use **JWT** / **API key** modes at the gateway (see ADR-002 and the gateway README).

**Vendor-specific notes (patterns, not product endorsements)**

| Platform | Role | Astrocyte plugin |
|----------|------|------------------|
| **Kong** | Routes, plugins (rate limit, JWT validation, OIDC), upstream to Kubernetes `Service` or VM pool | [`gateway-plugins/kong/`](../../gateway-plugins/kong/) — Lua plugin for recall + retain |
| **Apache APISIX** | Similar; use upstream health checks aligned with **`GET /health`** | [`gateway-plugins/apisix/`](../../gateway-plugins/apisix/) — Lua plugin for recall + retain |
| **Azure API Management** | Front door + policies; backend URL to your gateway **Service** / **Ingress** | [`gateway-plugins/azure-apim/`](../../gateway-plugins/azure-apim/) — XML policy fragments (Bicep/Terraform/APIOps) |
| **AWS API Gateway + ALB** | Public API → Lambda/container; map authorizer claims to headers the gateway JWT mode expects | — (not yet) |

Keep **Astrocyte-specific** policy (banks, `recall_authority`, PII) in **`astrocyte.yaml`**; use the API gateway for **coarse** throttling, IP allow lists, and **OAuth** / **JWT** issuance at the edge if that matches your IdP layout.

---

## 2. Edge hardening vs gateway env (split responsibility)

| Control | In `astrocyte-gateway-py` (env) | Often at edge instead |
|---------|-----------------------------------|------------------------|
| **CORS** | `ASTROCYTE_CORS_ORIGINS` (browser apps) | Same allowlist on APIM/Kong if browsers hit the edge URL |
| **Body size** | `ASTROCYTE_MAX_REQUEST_BODY_BYTES` (**413**) | Request size limits on ingress / WAF |
| **Admin routes** | `ASTROCYTE_ADMIN_TOKEN` + **`X-Admin-Token`** | Hide **`/v1/admin/*`** behind internal ingress or separate listener |
| **Rate limits** | `ASTROCYTE_RATE_LIMIT_PER_SECOND` (per client IP / **X-Forwarded-For**; **429**) | Global or tenant limits on API gateway; use app limit as a backstop |

**Liveness and readiness** — **`GET /live`**, **`GET /health`**, **`GET /health/ingest`** — are **exempt** from the in-process rate limiter so orchestration probes do not consume the API quota.

---

## 3. Quality gates: overhead benchmark and OpenAPI

- **Gateway overhead:** `astrocyte-services-py/astrocyte-gateway-py/scripts/bench_gateway_overhead.py` compares **`POST /v1/recall`** via ASGI (or **`--tcp`**) to direct **`brain.recall`**. Optional **`--max-overhead-p99-ms MS`** exits **2** if the p99 overhead exceeds **MS** (for optional CI SLO gates — tune **MS** per environment; mock LLMs are fast, production stores are not).
- **OpenAPI:** The running app serves **`/openapi.json`** and **`/docs`**. Contract tests in the gateway package assert Tier 1 paths remain present in the generated schema.

---

## 4. Connector track: transports, poll, and gateway plugins

**Shipped in-repo today:** **`astrocyte-ingestion-kafka`**, **`astrocyte-ingestion-redis`** (`type: stream`), **`astrocyte-ingestion-github`** (`type: poll`). Entry point groups: **`astrocyte.ingest_stream_drivers`**, **`astrocyte.ingest_poll_drivers`**.

### Gateway plugins (shipped)

Thin integration plugins that add long-term memory to LLM API calls routed through existing API gateways. Each plugin intercepts OpenAI-compatible `/chat/completions` requests and transparently calls the Astrocyte standalone gateway for **recall** (pre-hook) and **retain** (post-hook).

| Plugin | Gateway | Language | Location |
|--------|---------|----------|----------|
| **Kong** | [Kong Gateway](https://konghq.com/) | Lua | [`gateway-plugins/kong/`](../../gateway-plugins/kong/) |
| **APISIX** | [Apache APISIX](https://apisix.apache.org/) | Lua | [`gateway-plugins/apisix/`](../../gateway-plugins/apisix/) |
| **Azure APIM** | [Azure API Management](https://azure.microsoft.com/en-us/products/api-management) | XML policy fragments | [`gateway-plugins/azure-apim/`](../../gateway-plugins/azure-apim/) |

**When to use gateway plugins vs the standalone gateway directly:**

- Use a **gateway plugin** when your organization already routes LLM traffic through Kong, APISIX, or Azure APIM — zero agent code changes required.
- Use the **standalone gateway** directly for new deployments, or when you need reflect, event streams, or poll connectors (plugins are request-scoped).

See **[`gateway-plugins/README.md`](../../gateway-plugins/README.md)** for architecture, configuration, and limitations.

### Roadmap / deferred

- **NATS** (JetStream or core NATS) — same driver pattern as Kafka/Redis
- **Additional poll drivers** (ticketing, CRM APIs) — same pattern as GitHub Issues
- **Additional gateway plugins** (AWS API Gateway, Envoy, etc.) — same thin-shim pattern

See **[`product-roadmap-v1.md`](../_design/product-roadmap-v1.md)** § **v0.8.x — Connector & gateway integration track** and **[`adapters-ingestion-py/README.md`](../../adapters-ingestion-py/README.md)**.

---

## 5. Further reading

- **[Production-grade HTTP service](./production-grade-http-service.md)** — full checklist
- **[`astrocyte-gateway-py` README](../../astrocyte-services-py/astrocyte-gateway-py/README.md)** — env, auth, Compose
- **[Gateway plugins](../../gateway-plugins/README.md)** — Kong, APISIX, and Azure APIM integration plugins
- **[ADR-001](../_design/adr/adr-001-deployment-models.md)** — library vs standalone gateway
