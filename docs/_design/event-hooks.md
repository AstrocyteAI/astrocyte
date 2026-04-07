# Event hooks

Astrocyte emits events at key points in the memory lifecycle. Hooks allow external systems to react to these events - for audit logging, alerting, custom workflows, and enterprise integration.

---

## 1. Event types

| Event | When it fires | Payload includes |
|---|---|---|
| `on_retain` | After a successful retain | bank_id, memory_id, content_length, tags, policy_actions |
| `on_recall` | After a successful recall | bank_id, query, result_count, top_score, latency_ms |
| `on_reflect` | After a successful reflect | bank_id, query, answer_length, source_count, fallback_used |
| `on_forget` | After memories are deleted | bank_id, memory_ids, reason, compliance |
| `on_pii_detected` | When PII barrier detects sensitive content | bank_id, pii_type, action_taken (redact/reject/warn) |
| `on_rate_limited` | When a request is rate-limited | bank_id, operation, limit, retry_after |
| `on_circuit_breaker_open` | When provider circuit breaker trips | provider, failure_count, last_error |
| `on_circuit_breaker_close` | When provider recovers | provider, recovery_duration |
| `on_dedup_detected` | When signal quality detects a duplicate | bank_id, similarity_score, action_taken |
| `on_noisy_bank` | When analytics flags a noisy bank | bank_id, signal, threshold, action_taken |
| `on_ttl_expiry` | When TTL policy archives or deletes memories | bank_id, memory_count, action (archive/delete) |
| `on_consolidation` | When consolidation creates observations | bank_id, observation_count, source_fact_count |
| `on_bank_created` | When a new bank is created | bank_id, profile, created_by |
| `on_bank_deleted` | When a bank is deleted | bank_id, memory_count, deleted_by |
| `on_legal_hold` | When legal hold is set or released | bank_id, hold_id, action (set/released) |
| `on_import` | When memories are imported | bank_id, memory_count, source_provider |
| `on_export` | When memories are exported | bank_id, memory_count, destination_path |

### 1.1 Warehouse and lakehouse integration

Hook payloads align with the **`MemoryExportSink`** event taxonomy (`memory-export-sink.md`, `provider-spi.md` §5). **Today**, the practical path to land **Iceberg**, **Delta**, **Parquet**, or **warehouse SQL** tables is usually a **webhook** (§3.1) or **Python callable** (§3.3) that forwards events to your ingestor. When the core loads **`memory_export_sinks:`** from config, sinks should receive the same logical events in-process (at-least-once). Hooks **never** replace Tier 1 retrieval—they duplicate **metadata-oriented** signals for export and BI only unless you explicitly opt into content in payloads (§6).

---

## 2. Hook registration

> **Status:** YAML-based hook configuration (`hooks:` key) is a design target and not yet implemented.
> Currently, hooks are registered **programmatically** via the Python API:

```python
async def my_audit_hook(event: HookEvent) -> None:
    print(f"[{event.type}] bank={event.bank_id} data={event.data}")

brain.register_hook("on_retain", my_audit_hook)
brain.register_hook("on_pii_detected", my_audit_hook)
```

When YAML-based configuration ships, it will support webhook, log, and callable hook types as described in §3 below.

---

## 3. Hook types

### 3.1 Webhook

HTTP POST to an external URL.

```yaml
- type: webhook
  url: https://example.com/hook
  method: POST                           # POST (default) | PUT
  headers: {}                            # Custom headers
  body_template: null                    # Jinja2 template; if null, sends full event JSON
  timeout_ms: 5000
  retry_count: 2
  retry_delay_ms: 1000
  async: true                            # true = non-blocking (default), false = blocking
```

### 3.2 Log

Emit a structured log entry.

```yaml
- type: log
  level: info                            # debug | info | warning | error
  message: "{{event_type}}: {{bank_id}}"  # Optional template; default: full event JSON
```

### 3.3 Python callable (advanced)

For in-process hooks (not available in MCP server or the Rust implementation):

```python
from astrocyte import Astrocyte, HookEvent

async def my_custom_hook(event: HookEvent) -> None:
    if event.type == "on_pii_detected":
        await alert_security_team(event.data)

brain = Astrocyte.from_config("astrocyte.yaml")
brain.register_hook("on_pii_detected", my_custom_hook)
```

---

## 4. Event payload

All events share a common envelope:

```python
@dataclass
class HookEvent:
    event_id: str                        # Unique event ID (UUID)
    type: str                            # Event type (e.g., "on_retain")
    timestamp: datetime                  # When the event occurred
    bank_id: str | None = None           # Affected bank (if applicable)
    data: Metadata | None = None         # Event-specific payload (Metadata = dict[str, str|int|float|bool|None])
    trace_id: str | None = None          # OTel trace ID for correlation
```

Payloads must be portable (see `implementation-language-strategy.md`): only str, int, float, bool, None, list, dict. No callables or opaque Python objects in serialized event data.

---

## 5. Execution semantics

### 5.1 Async (default)

Hooks fire asynchronously after the operation completes. The operation's response is not delayed by hook execution. If a webhook fails, it is retried according to `retry_count` but does not affect the operation.

### 5.2 Sync (opt-in)

```yaml
- type: webhook
  url: https://compliance.company.com/audit
  async: false                           # Block until webhook returns 2xx
```

When `async: false`, the operation waits for the hook to complete. If the hook fails after retries, the operation still succeeds but a warning is logged. Use sparingly - this adds latency.

### 5.3 Failure handling

- **Async hooks**: failures are logged and emitted as OTel span events. No retry exhaustion alert by default (configure via `on_hook_failure` meta-hook if needed).
- **Sync hooks**: failures are logged, operation proceeds. The response includes a `hook_warnings` field.
- **Hooks never cause operations to fail.** A failed webhook does not roll back a retain or reject a recall.

---

## 6. Security

- Webhook URLs are validated at config load time (must be HTTPS in production, HTTP allowed in development).
- `body_template` uses sandboxed Jinja2 rendering (no file access, no code execution).
- Secrets in URLs or headers use environment variable substitution (`${VAR_NAME}`), never stored in plaintext.
- Hook payloads **never include memory content** by default. Only metadata (bank_id, memory_id, tags, scores). Content inclusion must be explicitly opted in:

```yaml
- type: webhook
  url: https://audit.company.com/full
  include_content: true                  # Opt-in: include memory text in payload
```

---

## 7. Built-in hooks

Some hooks are always active (not configurable):

| Hook | Always fires | Destination |
|---|---|---|
| All events | OTel span events | OpenTelemetry (if enabled) |
| Lifecycle events | Audit log | Audit trail (if enabled, see `memory-lifecycle.md`) |
| Prometheus counters | Metrics | Prometheus (if enabled) |

User-configured hooks add to these built-in hooks, they don't replace them.
