# Policy layer: the load-bearing astrocyte

This document specifies the governance policies that make the Astrocytes framework valuable regardless of which memory backend is plugged in. Each policy maps to a neuroscience design principle from `design-principles.md`.

The policy layer is not optional decoration. It is what makes the framework worth using instead of calling a memory backend directly. If these policies are thin wrappers, users will skip the framework. If they are solid, nobody will.

---

## 1. Homeostasis (Principle 3)

**Biology:** Astrocytes buffer extracellular potassium and regulate ion concentrations to prevent runaway neural excitation.

**Engineering:** Always-on constraints that prevent resource exhaustion, regardless of what the caller or provider does.

### 1.1 Token budgets

Every `recall()` and `reflect()` response is subject to a configurable token budget. The core counts tokens (via tiktoken or equivalent) and truncates results before returning to the caller.

```yaml
homeostasis:
  recall_max_tokens: 4096          # Hard ceiling on recall result tokens
  reflect_max_tokens: 8192         # Hard ceiling on reflect output
  retain_max_content_bytes: 102400 # Max content size per retain call (100KB)
```

This protects callers from providers that return unbounded results, and protects providers from callers that submit unbounded content. The limit is enforced in the core, not delegated to the provider.

### 1.2 Rate limiting

Per-bank and global rate limits prevent runaway loops or misconfigured agents from exhausting backend resources.

```yaml
homeostasis:
  rate_limits:
    retain_per_minute: 60
    recall_per_minute: 120
    reflect_per_minute: 20
    global_per_minute: 300
```

Implementation uses a token bucket algorithm. When a limit is hit, the core returns a `RateLimited` error with retry-after metadata - it does not silently queue or drop.

### 1.3 Quota enforcement

Long-running systems need cumulative limits in addition to per-minute rates.

```yaml
homeostasis:
  quotas:
    retain_per_day: 10000           # Max retain calls per bank per day
    reflect_per_day: 500            # Max reflect calls per bank per day
    storage_bytes_per_bank: null    # No limit (provider enforces its own)
```

Quota state is tracked in-process (resets on restart) or via an optional external store for distributed deployments.

---

## 2. Barriers (Principle 6)

**Biology:** Astrocyte end feet maintain the blood-brain barrier, controlling what substances cross into the neural environment.

**Engineering:** Content validation and sanitization on the retain path. What crosses into memory must be inspected.

### 2.1 PII barrier

> **Comprehensive coverage**: This section covers the PII scanning mechanism. For the full data governance framework - including data classification levels, PII detection taxonomy, per-type actions, regulatory compliance profiles (GDPR, HIPAA, PDPA), data residency, encryption, data lineage, and data loss prevention - see `data-governance.md`.

Scans content before it reaches the memory provider. Three modes:

| Mode | Mechanism | Dependency |
|---|---|---|
| `regex` | Pattern matching for emails, phones, SSNs, credit cards | None (built-in) |
| `ner` | Named entity recognition via lightweight model | Optional: spaCy or similar |
| `llm` | LLM classification of sensitive content | Requires LLM Provider SPI |

```yaml
barriers:
  pii:
    mode: regex                    # "regex" | "ner" | "llm" | "disabled"
    action: redact                 # "redact" | "reject" | "warn"
    patterns:                      # Additional patterns (regex mode)
      - name: custom_id
        pattern: "CUST-\\d{8}"
        replacement: "[REDACTED_CUSTOMER_ID]"
```

When `action: redact`, the core replaces detected PII with placeholders before forwarding to the provider. The original content never reaches the backend.

When `action: reject`, the core returns an error and does not forward the request.

When `action: warn`, the core forwards the request but emits a warning via observability.

### 2.2 Content validation

Structural checks that do not require PII detection:

```yaml
barriers:
  validation:
    max_content_length: 50000      # Characters
    reject_empty_content: true
    reject_binary_content: true
    allowed_content_types:         # Whitelist
      - text
      - conversation
      - document
```

### 2.3 Metadata sanitization

Strip or reject metadata keys that could leak across tenants or contain injection payloads:

```yaml
barriers:
  metadata:
    blocked_keys:                  # Keys that are never forwarded
      - api_key
      - password
      - token
      - secret
    max_metadata_size_bytes: 4096
```

---

## 3. Signal quality (Principle 7)

**Biology:** Astrocytes participate in synaptic pruning, clearing debris and low-quality connections to maintain circuit health.

**Engineering:** Evaluate content quality before storage. Prevent memory pollution from noisy, redundant, or low-value data.

### 3.1 Deduplication

Before forwarding a retain request, check for near-duplicate content already stored. This requires a lightweight recall to the provider.

```yaml
signal_quality:
  dedup:
    enabled: true
    similarity_threshold: 0.95     # Cosine similarity for near-match
    action: skip                   # "skip" | "warn" | "update"
```

When `action: skip`, duplicates are silently dropped. The RetainResult indicates the content was deduplicated, not stored.

### 3.2 Quality scoring

Optional LLM-based evaluation of content value before storage:

```yaml
signal_quality:
  scoring:
    enabled: false                 # Disabled by default (requires LLM provider)
    min_score: 0.3                 # Below this, content is rejected
    action: reject                 # "reject" | "tag" | "warn"
```

When `action: tag`, low-quality content is stored but tagged with `_quality: low` for downstream filtering.

### 3.3 Noisy bank detection

Track per-bank metrics over time. Flag banks with anomalous patterns:

- Retain rate suddenly spikes (runaway agent loop)
- Average content length drops below threshold (junk)
- Dedup hit rate exceeds threshold (redundant)

```yaml
signal_quality:
  noisy_bank:
    enabled: true
    retain_spike_multiplier: 5.0   # Flag if rate is 5x rolling average
    min_avg_content_length: 20     # Characters
    max_dedup_rate: 0.8            # 80% of retains are duplicates
    action: warn                   # "warn" | "throttle" | "reject"
```

---

## 4. Escalation and degraded mode (Principle 8)

**Biology:** Microglial signals can push astrocytes into reactive states; positive feedback can amplify damage. De-escalation is essential.

**Engineering:** When the memory backend is unhealthy or unavailable, escalate through controlled channels. Provide de-escalation once the backend recovers.

### 4.1 Circuit breaker

Wraps all provider calls. Trips after consecutive failures, preventing cascading requests to a failing backend.

```yaml
escalation:
  circuit_breaker:
    failure_threshold: 5           # Consecutive failures before tripping
    recovery_timeout_seconds: 30   # Time before attempting recovery
    half_open_max_calls: 2         # Test calls during recovery
```

States: `closed` (normal) → `open` (all calls fail-fast) → `half-open` (test recovery) → `closed`.

### 4.2 Degraded mode

What happens when the circuit breaker is open:

```yaml
escalation:
  degraded_mode: empty_recall      # "empty_recall" | "error" | "cache"
```

| Mode | Behavior |
|---|---|
| `empty_recall` | `recall()` returns empty results. `retain()` queues for retry. `reflect()` returns "memory unavailable" answer. System remains functional with reduced capability. |
| `error` | All operations return `ProviderUnavailable`. Caller must handle. Fail-closed. |
| `cache` | Return stale results from a local cache (if enabled). Best-effort. |

### 4.3 De-escalation

When the circuit breaker transitions from `open` → `half-open` → `closed`, the core:

1. Drains any queued retain operations (if `empty_recall` mode buffered them).
2. Emits an OTel event marking recovery.
3. Resets noisy bank counters (the spike was likely caused by the outage, not the bank).

This mirrors the biological need to avoid chronic A1-like reactive lock-in. The system must return to homeostasis, not stay in alert mode.

---

## 5. Observability (Principle 9)

**Biology:** Modern astrocyte science relies on imaging, scRNA-seq, and genetics to measure state - not assume uniformity.

**Engineering:** Expose metrics and traces for the regulatory layer itself. Monitor the milieu, not only the outputs.

### 5.1 OpenTelemetry spans

Every operation gets an OTel span with standardized attributes:

```
astrocytes.retain
  ├── astrocytes.bank_id
  ├── astrocytes.provider
  ├── astrocytes.content_bytes
  ├── astrocytes.pii_detected (bool)
  ├── astrocytes.dedup_hit (bool)
  ├── astrocytes.policy.actions_taken (list)
  └── provider.retain (child span, provider's own timing)

astrocytes.recall
  ├── astrocytes.bank_id
  ├── astrocytes.provider
  ├── astrocytes.query_tokens
  ├── astrocytes.result_count
  ├── astrocytes.result_tokens
  ├── astrocytes.truncated (bool)
  ├── astrocytes.fallback_used (bool)
  └── provider.recall (child span)

astrocytes.reflect
  ├── astrocytes.bank_id
  ├── astrocytes.provider
  ├── astrocytes.native_reflect (bool, or fallback)
  ├── astrocytes.llm_tokens_used (if fallback)
  └── provider.reflect OR astrocytes.fallback_reflect (child span)
```

Spans propagate the caller's trace context, so Astrocytes operations appear in the agent's distributed trace.

### 5.2 Prometheus metrics

| Metric | Type | Labels |
|---|---|---|
| `astrocytes_retain_total` | Counter | `bank_id`, `provider`, `status` |
| `astrocytes_recall_total` | Counter | `bank_id`, `provider`, `status` |
| `astrocytes_reflect_total` | Counter | `bank_id`, `provider`, `fallback` |
| `astrocytes_retain_duration_seconds` | Histogram | `bank_id`, `provider` |
| `astrocytes_recall_duration_seconds` | Histogram | `bank_id`, `provider` |
| `astrocytes_pii_detected_total` | Counter | `bank_id`, `action` |
| `astrocytes_dedup_total` | Counter | `bank_id`, `action` |
| `astrocytes_circuit_breaker_state` | Gauge | `provider` (0=closed, 1=open, 2=half-open) |
| `astrocytes_rate_limit_rejections_total` | Counter | `bank_id`, `operation` |
| `astrocytes_token_budget_truncations_total` | Counter | `bank_id`, `operation` |

### 5.3 Structured logging

All policy actions emit structured log entries (JSON) with consistent fields:

```json
{
  "event": "astrocytes.policy.pii_redacted",
  "bank_id": "user-123",
  "provider": "mem0",
  "pattern": "email",
  "action": "redact",
  "trace_id": "abc123"
}
```

Users who switch memory providers keep their dashboards, alerts, and log queries intact. This is a major value proposition of the framework.

---

## 6. Policy configuration reference

All policies are configured under one config file and can be overridden per-bank:

```yaml
# astrocytes.yaml
provider: mystique
llm_provider: anthropic

homeostasis:
  recall_max_tokens: 4096
  reflect_max_tokens: 8192
  retain_max_content_bytes: 102400
  rate_limits:
    retain_per_minute: 60
    recall_per_minute: 120
    reflect_per_minute: 20

barriers:
  pii:
    mode: regex
    action: redact
  validation:
    max_content_length: 50000
    reject_empty_content: true

signal_quality:
  dedup:
    enabled: true
    similarity_threshold: 0.95
  noisy_bank:
    enabled: true
    action: warn

escalation:
  circuit_breaker:
    failure_threshold: 5
    recovery_timeout_seconds: 30
  degraded_mode: empty_recall

observability:
  otel_enabled: true
  prometheus_enabled: true
  log_level: info

# Per-bank overrides
banks:
  sensitive-customer:
    barriers:
      pii:
        mode: llm
        action: reject
    homeostasis:
      retain_per_minute: 10
```

---

## 7. Policy execution order

Policies execute in a fixed order on each request path:

### Retain path (inbound)

```
1. Rate limit check          (homeostasis)
2. Content validation         (barrier)
3. PII scanning               (barrier)
4. Metadata sanitization      (barrier)
5. Signal quality / dedup     (pruning)
6. Start OTel span            (observability)
7. Forward to provider
8. Close OTel span
9. Return result
```

### Recall path (outbound)

```
1. Rate limit check          (homeostasis)
2. Query sanitization         (barrier)
3. Start OTel span            (observability)
4. Forward to provider (with circuit breaker)
5. Token budget enforcement   (homeostasis)
6. Close OTel span
7. Return result
```

### Reflect path

```
1. Rate limit check          (homeostasis)
2. Query sanitization         (barrier)
3. Start OTel span            (observability)
4. Capability check
5a. Provider reflect (if supported) - with circuit breaker
5b. Fallback: recall + LLM synthesis (if configured)
5c. Degrade: recall only (if configured)
6. Token budget enforcement   (homeostasis)
7. Close OTel span
8. Return result
```

The order is intentional: cheap checks (rate limits, validation) run first. Expensive checks (PII scanning, dedup) run after validation passes. Provider calls are wrapped in circuit breakers. Observability wraps the provider call specifically so span duration reflects backend latency.
