# Bank management

Practical recipes for organizing memory banks -- creating, configuring, multi-bank queries, and lifecycle patterns.

---

## What is a bank?

A bank is an isolated namespace for memories. Banks provide tenant isolation, access control boundaries, and policy scoping. Each bank has its own configuration surface (rate limits, barriers, signal quality) and can be queried independently or together.

Banks are created implicitly on first `retain()` or explicitly in config.

---

## Creating banks

### Implicit (on first use)

No upfront setup required. The bank is created automatically when the first memory is stored.

```python
# Bank "user-alice" is created automatically
await brain.retain("Alice likes Python", bank_id="user-alice")
```

### Explicit (in config)

Define banks in `astrocyte.yaml` with their own access grants, homeostasis rules, and barriers.

```yaml
banks:
  team-engineering:
    profile: default
    access:
      - principal: "user:*"
        permissions: [read, write]
    homeostasis:
      rate_limits:
        retain_per_minute: 120
    barriers:
      pii_scan: true

  sensitive-data:
    profile: hipaa
    barriers:
      pii_scan: true
      pii_action: redact
```

BankConfig fields: `profile`, `access`, `homeostasis`, `barriers`, `signal_quality`.

### Convention-based (identity-driven)

Let Astrocyte resolve bank IDs from caller identity automatically.

```yaml
identity:
  auto_resolve_banks: true
  user_bank_prefix: "user-"
  agent_bank_prefix: "agent-"
  resolver: convention
```

User `alice` maps to bank `user-alice`. Agent `ingester` maps to bank `agent-ingester`. Banks are created on first use if they do not already exist.

---

## Listing banks

The admin endpoint lists all banks with their configuration and health summary.

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
  http://localhost:8080/v1/admin/banks
```

```json
{
  "banks": [
    {"bank_id": "user-alice", "memory_count": 342, "created_at": "2026-03-01T..."},
    {"bank_id": "team-engineering", "memory_count": 1204, "created_at": "2026-01-15T..."}
  ]
}
```

Requires `X-Admin-Token` header. See [Authentication setup](authentication-setup.md) for token configuration.

---

## Multi-bank queries

### Parallel (default)

Query all listed banks simultaneously, fuse results by relevance score.

```python
result = await brain.recall(
    "deployment strategy",
    banks=["team-eng", "team-ops", "shared"],
    strategy="parallel",
    max_results=10,
)
```

### Cascade

Widen the search through banks in order, stopping when enough results are found.

```python
from astrocyte.types import MultiBankStrategy

result = await brain.recall(
    "deployment strategy",
    banks=["team-eng", "team-ops", "shared"],
    strategy=MultiBankStrategy(
        mode="cascade",
        min_results_to_stop=5,
        cascade_order=["team-eng", "team-ops", "shared"],
    ),
)
```

Cascade checks `team-eng` first. If fewer than 5 results, it adds `team-ops`. If still short, it adds `shared`.

### First match

Return results from the first bank that produces any hits.

```python
result = await brain.recall(
    "deployment strategy",
    banks=["personal", "team", "global"],
    strategy="first_match",
)
```

Useful for layered overrides where personal preferences take priority over team or global defaults.

---

## Common patterns

### Personal banks (multi-tenant SaaS)

Each user gets their own bank. The convention resolver auto-creates banks from caller identity.

```yaml
identity:
  auto_resolve_banks: true
  user_bank_prefix: "user-"
  resolver: convention

access_grants:
  - bank_id: "user-*"
    principal: "user:${bank_owner}"
    permissions: [read, write, forget]
```

User `bob` writes to `user-bob`. No user can read another user's bank unless explicitly granted.

### Team shared + personal overlay

A shared team bank for collective knowledge, with personal banks for individual preferences.

```yaml
banks:
  team-platform:
    access:
      - principal: "user:*"
        permissions: [read, write]

access_grants:
  - bank_id: "user-*"
    principal: "user:${bank_owner}"
    permissions: [read, write, forget]
```

Recall with cascade so personal preferences win:

```python
result = await brain.recall(
    "preferred CI provider",
    banks=["user-alice", "team-platform", "global"],
    strategy=MultiBankStrategy(
        mode="cascade",
        min_results_to_stop=1,
        cascade_order=["user-alice", "team-platform", "global"],
    ),
)
```

### Agent-scoped banks

Each agent gets its own bank for task-specific memory, preventing cross-contamination.

```yaml
identity:
  auto_resolve_banks: true
  agent_bank_prefix: "agent-"
  resolver: convention

access_grants:
  - bank_id: "agent-*"
    principal: "agent:${bank_owner}"
    permissions: [read, write]
  - bank_id: "agent-*"
    principal: "user:admin"
    permissions: [read, admin]
```

### Time-partitioned banks

Monthly or quarterly banks for archival and bounded storage.

```python
from datetime import datetime

bank_id = f"logs-{datetime.now():%Y-%m}"
await brain.retain(content, bank_id=bank_id)
```

Combine with lifecycle rules to auto-prune old partitions:

```python
# Clean up banks older than 6 months
await brain.forget(bank_id="logs-2025-10", scope="all")
```

### Read-only reference banks

A curated knowledge bank that agents can read but not write to.

```yaml
banks:
  company-policies:
    access:
      - principal: "agent:*"
        permissions: [read]
      - principal: "user:knowledge-admin"
        permissions: [read, write, admin]
    barriers:
      pii_scan: true
```

Only `knowledge-admin` can update the bank. All agents can query it.

---

## Per-bank overrides

Banks can override instance-level configuration for:

- **homeostasis** -- rate limits, concurrency
- **barriers** -- PII scanning, validation rules
- **signal_quality** -- relevance thresholds, dedup sensitivity
- **access** -- per-bank grants

Example: a bank with stricter PII scanning than the instance default.

```yaml
banks:
  healthcare-notes:
    barriers:
      pii_scan: true
      pii_action: redact
      pii_categories: [name, ssn, dob, medical_record]
    homeostasis:
      rate_limits:
        retain_per_minute: 30
    signal_quality:
      min_relevance: 0.8
```

The instance default might allow PII through with a warning. This bank redacts it unconditionally.

---

## Bank lifecycle

| Stage | How |
|-------|-----|
| **Creation** | Implicit on first `retain()`, or explicit in `astrocyte.yaml` |
| **Growth** | Monitor via bank health metrics (memory count, storage size) |
| **Pruning** | `forget()` with `before_date` for time-based cleanup |
| **Archival** | Export memories via memory export sink before deleting |
| **Deletion** | `forget(bank_id="x", scope="all")` -- requires `admin` permission |

Pruning example:

```python
from datetime import datetime, timedelta

cutoff = datetime.now() - timedelta(days=90)
await brain.forget(bank_id="user-alice", before_date=cutoff)
```

Full deletion:

```python
await brain.forget(bank_id="temp-experiment", scope="all")
```

This removes all memories and the bank entry itself. The operation is irreversible and requires the `admin` permission on the target bank.

---

## Bank health

Use the `/health` endpoint with a bank filter, or query Prometheus metrics per bank.

```bash
curl http://localhost:8080/health?bank_id=team-engineering
```

Key metrics exposed per bank:

| Metric | Description |
|--------|-------------|
| `astrocyte_bank_memory_count` | Total memories stored |
| `astrocyte_bank_storage_bytes` | Approximate storage used |
| `astrocyte_bank_retain_total` | Cumulative retain operations |
| `astrocyte_bank_recall_total` | Cumulative recall operations |
| `astrocyte_bank_recall_latency_seconds` | Recall latency histogram |

See [Monitoring & observability](monitoring-and-observability.md) for dashboard setup and alerting.

---

## Further reading

- [Memory API reference](memory-api-reference/) — retain/recall/reflect/forget signatures
- [Access control setup](access-control-setup/) — principals, grants, and per-bank permissions
- [Configuration reference](configuration-reference/) — full `astrocyte.yaml` key reference
- [Monitoring & observability](monitoring-and-observability/) — health endpoints, metrics, alerting
- [Multi-bank orchestration](/design/multi-bank-orchestration/) — design doc with strategy details and RRF fusion
