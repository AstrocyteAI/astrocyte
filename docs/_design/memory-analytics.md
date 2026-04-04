# Bank health & utilization

The observability layer (see `policy-layer.md`) captures per-operation metrics and traces. This document describes **in-process memory analytics**: **system-level health assessment**—whether memory is helping agents or just accumulating noise. For warehouse / lakehouse **export** (append-only events to external tables), see [`memory-export-sink.md`](./memory-export-sink.md) and [`storage-and-data-planes.md`](./storage-and-data-planes.md); do not confuse that SPI with bank health metrics here.

This maps to **Principle 9 (Observable state)** taken to its full conclusion: not just tracing operations, but understanding the health of the memory system as a whole.

---

## 1. Bank health metrics

Each bank gets a health score derived from usage patterns.

### 1.1 Metrics collected

| Metric | What it measures | Healthy range |
|---|---|---|
| `recall_hit_rate` | % of recalls that return >=1 result | 60-95% |
| `avg_recall_score` | Average relevance score of top hit | 0.5-0.9 |
| `retain_rate` | Retains per hour | Depends on use case |
| `dedup_rate` | % of retains that are near-duplicates | <20% |
| `avg_content_length` | Average retained content length (chars) | >50 |
| `recall_to_retain_ratio` | How often memory is read vs written | >0.5 |
| `reflect_success_rate` | % of reflects that produce a non-empty answer | >80% |
| `memory_count` | Total memories in bank | Depends on use case |
| `entity_count` | Unique entities tracked | Growing over time |
| `staleness` | % of memories never recalled in last 30 days | <70% |

### 1.2 Health score calculation

```python
@dataclass
class BankHealth:
    bank_id: str
    score: float                         # 0.0 (unhealthy) to 1.0 (healthy)
    status: Literal["healthy", "warning", "unhealthy"]
    issues: list[HealthIssue]
    metrics: dict[str, float]
    assessed_at: datetime

@dataclass
class HealthIssue:
    severity: Literal["info", "warning", "critical"]
    code: str                            # e.g., "HIGH_DEDUP_RATE", "LOW_RECALL_HIT_RATE"
    message: str
    recommendation: str
```

### 1.3 API

```python
health = await brain.bank_health("user-123")
# → BankHealth(score=0.82, status="healthy", issues=[...])

# All banks
healths = await brain.all_bank_health()
# → [BankHealth(...), BankHealth(...), ...]
```

---

## 2. Noisy agent detection

Agents in loops or with misconfigured auto-retain can flood memory with low-value content. Analytics detects this.

### 2.1 Detection signals

| Signal | Threshold (configurable) | Meaning |
|---|---|---|
| Retain rate spike | >5x rolling 1-hour average | Agent is in a loop |
| Dedup rate spike | >80% of retains are duplicates | Agent is re-storing the same content |
| Content length drop | Average <20 chars over 100 retains | Agent is storing junk |
| Recall hit rate drop | <20% over 100 recalls | Memories are not relevant to queries |

### 2.2 Actions

```yaml
analytics:
  noisy_agent:
    enabled: true
    action: warn                         # "warn" | "throttle" | "pause"
    throttle_to_percent: 10              # If action=throttle, allow 10% of retains through
    alert_hook: on_noisy_agent_detected  # Trigger event hook (see event-hooks.md)
```

---

## 3. Memory utilization analysis

Understanding what's actually useful in memory.

### 3.1 Utilization report

```python
report = await brain.utilization_report("user-123")
```

```python
@dataclass
class UtilizationReport:
    bank_id: str
    total_memories: int
    active_memories: int                 # Recalled at least once in last 30 days
    stale_memories: int                  # Never recalled in last 30 days
    orphaned_entities: int               # Entities with no linked memories
    top_recalled: list[MemoryUsage]      # Most frequently recalled memories
    never_recalled: int                  # Memories never recalled since creation
    fact_type_distribution: dict[str, int]  # {"world": 120, "experience": 85, ...}
    tag_distribution: dict[str, int]     # {"preference": 40, "technical": 60, ...}
    storage_estimate_bytes: int          # Approximate storage used
    period: tuple[datetime, datetime]    # Analysis period

@dataclass
class MemoryUsage:
    memory_id: str
    text: str                            # First 100 chars
    recall_count: int
    last_recalled_at: datetime
```

### 3.2 Quality trends

Track memory quality over time:

```python
trends = await brain.quality_trends(
    bank_id="user-123",
    period_days=30,
    granularity="daily",
)
```

```python
@dataclass
class QualityTrends:
    bank_id: str
    data_points: list[QualityDataPoint]

@dataclass
class QualityDataPoint:
    date: date
    retain_count: int
    recall_count: int
    recall_hit_rate: float
    avg_recall_score: float
    dedup_rate: float
    reflect_success_rate: float
```

---

## 4. Prometheus metrics (analytics-specific)

In addition to the per-operation metrics in `policy-layer.md`, analytics exports:

| Metric | Type | Labels |
|---|---|---|
| `astrocytes_bank_health_score` | Gauge | `bank_id` |
| `astrocytes_bank_memory_count` | Gauge | `bank_id`, `fact_type` |
| `astrocytes_bank_stale_memory_percent` | Gauge | `bank_id` |
| `astrocytes_bank_dedup_rate` | Gauge | `bank_id` |
| `astrocytes_bank_recall_hit_rate` | Gauge | `bank_id` |
| `astrocytes_noisy_agent_detected_total` | Counter | `bank_id`, `action` |

---

## 5. Dashboard integration

Analytics metrics are designed for Grafana or equivalent dashboards:

- **Bank overview**: health score, memory count, utilization across all banks
- **Agent activity**: retain/recall rates per bank, noisy agent alerts
- **Quality trends**: recall hit rate and dedup rate over time
- **Lifecycle status**: memories by state (active, archived, deleted), TTL expirations

---

## 6. Configuration

```yaml
analytics:
  enabled: true
  health_check_schedule: "*/15 * * * *"  # Every 15 minutes
  utilization_report_schedule: "0 4 * * *"  # 4am daily
  trends_retention_days: 90               # Keep trend data for 90 days
  noisy_agent:
    enabled: true
    action: warn
```

---

## 7. Durable analytical plane (warehouses and lakehouses)

In-process **bank health**, utilization, and Prometheus metrics above serve **operational** observability. For **warehouse-scale** history, BI, and compliance over **SQL**, **Parquet**, **Iceberg**, **Delta**, and similar layouts, use the **Memory Export Sink** SPI and **`memory_export_sinks:`** config (`memory-export-sink.md`, `ecosystem-and-packaging.md`). Sinks complement—do not replace—this module: sinks focus on **append-only event streams** to external tables; this document focuses on **scores and dashboards** inside the Astrocytes process.
