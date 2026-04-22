# Monitoring & observability

Health endpoints, structured logging, request tracing, metrics, and alerting for Astrocyte in production.

---

## Health endpoints

Three endpoints for liveness, readiness, and ingest health. All are exempt from rate limiting.

### GET /live — liveness probe

Returns 200 if the process is running. No dependency checks.

```bash
curl http://localhost:8080/live
```

```json
{"status": "ok"}
```

Use for Kubernetes liveness probes — if this fails, the process is dead.

### GET /health — readiness probe

Checks all critical dependencies (vector store, database). Returns latency for SLO tracking.

```bash
curl http://localhost:8080/health
```

**Healthy (200):**

```json
{
  "healthy": true,
  "message": "healthy",
  "latency_ms": 45.2,
  "last_check_at": "2026-04-13T10:30:45.123456+00:00"
}
```

**Unhealthy (503):**

```json
{
  "detail": "Health check timed out (dependencies such as the vector store did not respond in time)."
}
```

The health check has an **8-second timeout** — if dependencies don't respond in time, the endpoint returns 503 immediately to prevent load balancer hangs.

Use for Kubernetes readiness probes and load balancer health checks.

### GET /health/ingest — ingest pipeline health

Monitors ingest sources (poll, stream, webhook) independently of core storage.

```bash
curl http://localhost:8080/health/ingest
```

**All healthy (200):**

```json
{
  "status": "ok",
  "aggregate": {
    "healthy": true,
    "message": "all ingest sources healthy"
  },
  "sources": [
    {"id": "github_issues", "type": "poll", "healthy": true, "message": "Last poll succeeded"},
    {"id": "redis_events", "type": "stream", "healthy": true, "message": "Consumer group lag: 5"}
  ]
}
```

**Degraded (200 with `status: "degraded"`):**

```json
{
  "status": "degraded",
  "aggregate": {
    "healthy": false,
    "message": "unhealthy sources: github_issues"
  },
  "sources": [
    {"id": "github_issues", "type": "poll", "healthy": false, "message": "GitHub API rate limit exceeded"}
  ]
}
```

**No sources configured:**

```json
{
  "status": "ok",
  "aggregate": {"healthy": true, "message": "no ingest sources configured"},
  "sources": []
}
```

This endpoint is public (no admin token required). For detailed source admin info, use `GET /v1/admin/sources` with `X-Admin-Token`.

---

## Kubernetes probe configuration

```yaml
livenessProbe:
  httpGet:
    path: /live
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
  timeoutSeconds: 2

readinessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 5
  timeoutSeconds: 8          # must be <= 8s (gateway health timeout)
  failureThreshold: 2
```

---

## Structured logging

### Enable JSON logging

```bash
export ASTROCYTE_LOG_FORMAT=json
export ASTROCYTE_LOG_LEVEL=INFO       # DEBUG, INFO, WARNING, ERROR
```

Valid values for `ASTROCYTE_LOG_FORMAT`: `json`, `1`, `true`, `yes`. Unset or any other value uses human-readable text.

### Access logs

Every HTTP request is logged with request ID, method, path, status, and latency.

**Text (default):**

```
2026-04-13 10:30:45 - astrocyte_gateway.access - INFO
a1b2c3d4-e5f6-7890-abcd-ef1234567890 POST /v1/recall 200 45.23ms
```

**JSON (`ASTROCYTE_LOG_FORMAT=json`):**

```json
{
  "ts": "2026-04-13T10:30:45.123+00:00",
  "level": "INFO",
  "logger": "astrocyte_gateway.access",
  "msg": "{\"event\": \"http_request\", \"request_id\": \"a1b2c3d4\", \"method\": \"POST\", \"path\": \"/v1/recall\", \"status_code\": 200, \"duration_ms\": 45.23}"
}
```

### Ingest event logs

Ingest sources emit structured events that respect `ASTROCYTE_LOG_FORMAT`:

**Text:** `source_id='github_issues' event='poll_started' interval_seconds=3600`

**JSON:** `{"event": "poll_started", "source_id": "github_issues", "interval_seconds": 3600}`

Common ingest events: `source_started`, `source_stopped`, `poll_success`, `poll_failure`, `rate_limit_exceeded`, `stream_lag`, `transport_error`.

### Policy event logs

The policy layer emits structured events for barrier enforcement, rate limiting, and circuit breaker state changes:

```json
{
  "event": "policy_barrier_applied",
  "timestamp": "2026-04-13T10:30:45.123456+00:00",
  "bank_id": "user-123",
  "operation": "retain",
  "data": {"pii_detected": true, "pii_types": ["email", "phone"], "latency_ms": 12.5}
}
```

---

## Request tracking

### Request IDs

Every request gets a unique ID. If the client sends `X-Request-ID`, it's echoed back. Otherwise a UUID4 is generated.

```bash
# Client sends a request ID
curl -H "X-Request-ID: my-trace-123" http://localhost:8080/v1/recall ...
# Response includes: X-Request-ID: my-trace-123

# No client ID — gateway generates one
curl http://localhost:8080/v1/recall ...
# Response includes: X-Request-ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

Use request IDs to correlate logs across load balancers, proxies, and the gateway.

---

## OpenTelemetry (distributed tracing)

### Setup

Install the optional `[otel]` extras and enable via env var:

```bash
pip install astrocyte-gateway-py[otel]
# or: uv sync --extra otel

export ASTROCYTE_OTEL_ENABLED=1
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_SERVICE_NAME=astrocyte-gateway
export OTEL_RESOURCE_ATTRIBUTES=environment=prod,version=0.8.0
```

### What gets traced

- All FastAPI HTTP routes (automatic via `FastAPIInstrumentor`)
- Request/response details, latency, exceptions
- Core policy operations via `span()` context manager (PII scanning, recall, retain, etc.)

### Collector setup

Traces are exported via OTLP HTTP to any compatible collector:

| Collector | Endpoint example |
|-----------|-----------------|
| **Jaeger** | `http://jaeger:4317` |
| **Grafana Tempo** | `http://tempo:4317` |
| **Datadog** | `http://datadog-agent:4317` |
| **AWS X-Ray** (via ADOT) | `http://adot-collector:4317` |

If OpenTelemetry is not installed or not enabled, all tracing is a no-op — zero overhead.

### Env var reference

| Variable | Default | Description |
|----------|---------|-------------|
| `ASTROCYTE_OTEL_ENABLED` | unset | Set to `1`, `true`, or `yes` to enable |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP collector endpoint |
| `OTEL_SERVICE_NAME` | `astrocyte-gateway-py` | Service name in traces |
| `OTEL_RESOURCE_ATTRIBUTES` | unset | Comma-separated `key=value` pairs (e.g. `environment=prod`) |

---

## Prometheus metrics

### Setup

Install `prometheus-client` and enable in `astrocyte.yaml`:

```bash
pip install prometheus-client
```

```yaml
observability:
  prometheus_enabled: true
```

### Available metric types

The `MetricsCollector` in the policy layer supports counters, histograms, and gauges:

| Metric type | Example name | Description |
|-------------|-------------|-------------|
| Counter | `astrocyte_recalls_total` | Total recall operations by bank and status |
| Counter | `astrocyte_retains_total` | Total retain operations |
| Histogram | `astrocyte_recall_latency_ms` | Recall latency distribution |
| Histogram | `astrocyte_retain_latency_ms` | Retain latency distribution |
| Gauge | `astrocyte_bank_memory_count` | Current memory count per bank |

If `prometheus-client` is not installed, metrics collection is a no-op.

---

## Rate limiting

### Edge rate limiting (per-client)

Set `ASTROCYTE_RATE_LIMIT_PER_SECOND` to enable a sliding-window rate limiter at the HTTP layer:

```bash
export ASTROCYTE_RATE_LIMIT_PER_SECOND=100
```

- **Scope:** Per client IP address (TCP peer, not `X-Forwarded-For`)
- **Window:** 1 second (sliding)
- **Response:** 429 with `Retry-After: 1` header
- **Exempt:** `/live`, `/health`, `/health/*` (all health endpoints)
- **Memory safety:** Tracks up to 5,000 clients; returns 429 if table is full

For internet-facing deployments, prefer rate limiting at the edge (Kong, APISIX, Azure APIM) and use this as a backstop.

### Application rate limiting (per-bank)

Configured in `astrocyte.yaml` under `homeostasis.rate_limits`:

```yaml
homeostasis:
  rate_limits:
    retain_per_minute: 60
    recall_per_minute: 120
    reflect_per_minute: 30
```

Uses token bucket algorithm per bank per operation. Returns 429 with `retry_after_seconds` when exhausted.

---

## Alerting recommendations

| Alert | Condition | Indicates |
|-------|-----------|-----------|
| **Readiness failure** | `/health` returns 503 for > 1 minute | Database or vector store is down |
| **High latency** | p99 latency for `/v1/recall` > 2 seconds | Slow backend or provider API |
| **Error spike** | > 5% of requests return 5xx | Crashes, config errors, or dependency failures |
| **Rate limit exhaustion** | Sustained 429 responses from per-bank limiter | Client hammering a single bank |
| **Ingest degraded** | `/health/ingest` status is `degraded` | Poll/stream/webhook source is failing |
| **Circuit breaker open** | Policy logs show `circuit_breaker_opened` event | Backend has hit failure threshold |

---

## Operator checklist

- [ ] **Liveness probe** — `/live` configured (Kubernetes or load balancer)
- [ ] **Readiness probe** — `/health` configured with 8s timeout
- [ ] **Ingest health** — `/health/ingest` monitored if using poll/stream/webhook
- [ ] **JSON logging** — `ASTROCYTE_LOG_FORMAT=json` enabled for log aggregation
- [ ] **Log shipping** — Logs flowing to centralized aggregator (ELK, Datadog, CloudWatch)
- [ ] **Request IDs** — Confirmed in response headers (enabled by default)
- [ ] **Rate limiting** — Edge limiter at reverse proxy; in-process limiter as backstop
- [ ] **OpenTelemetry** — Enabled if distributed tracing is needed (`ASTROCYTE_OTEL_ENABLED=1`)
- [ ] **Dashboards** — Request rate, error rate, latency (p50/p95/p99) per route
- [ ] **Alerts** — Health failures, latency spikes, error rate, rate limit exhaustion, ingest degradation

---

## Environment variable summary

| Variable | Default | Description |
|----------|---------|-------------|
| `ASTROCYTE_LOG_FORMAT` | unset | `json` for structured JSON logging |
| `ASTROCYTE_LOG_LEVEL` | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |
| `ASTROCYTE_OTEL_ENABLED` | unset | `1` to enable OpenTelemetry |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP collector endpoint |
| `OTEL_SERVICE_NAME` | `astrocyte-gateway-py` | Service name in traces |
| `OTEL_RESOURCE_ATTRIBUTES` | unset | Resource attributes (`key=value,...`) |
| `ASTROCYTE_RATE_LIMIT_PER_SECOND` | unset | Per-client HTTP rate limit |
| `ASTROCYTE_ADMIN_TOKEN` | unset | Token for `/v1/admin/*` routes |

---

## Further reading

- [Configuration reference](configuration-reference/) — `observability`, `homeostasis`, and `escalation` config sections
- [Memory API reference](memory-api-reference/) — retain/recall/reflect/forget signatures
- [Bank management](bank-management/) — per-bank metrics and health monitoring
- [Production-grade HTTP service](production-grade-http-service/) — full production checklist
- [Gateway edge & API gateways](gateway-edge-and-api-gateways/) — edge rate limiting and reverse proxy patterns
