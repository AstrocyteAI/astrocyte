# Memory export sink (warehouse, lakehouse, open table formats)

This document specifies the **durable export / analytical persistence plane** for Astrocytes: optional, **orthogonal** to Tier 1 retrieval (`VectorStore` / `GraphStore` / `DocumentStore`) and to Tier 2 memory engines.

**Purpose:** stream or batch **governed memory events and snapshots** into **data warehouses**, **lakehouses**, and **open table formats** (for example **Apache Iceberg**, **Delta Lake**, **Apache Hudi**, **Apache Paimon**; **Apache Parquet** as the on-disk columnar layout; **SQL-accessible** tables in Snowflake, BigQuery, Redshift, Fabric, Doris, …) for BI, compliance, model training, and historical analysis.

**Non-goals:** This plane does **not** replace `brain.recall()` online path. It does **not** define similarity search over lake files **by itself**. Offline or batch SQL over exported tables is normal; **agent-time** hybrid recall still needs a **Tier 1** (or Tier 2) path—often implemented against a **serving** or query API **above** the same lake/warehouse (`storage-and-data-planes.md` §1). Downstream jobs (SQL, Spark, Flink, Trino) consume sink data for analytics; they are not a substitute for the export SPI unless you explicitly design a different product.

**Related:** [`storage-and-data-planes.md`](./storage-and-data-planes.md) (hub), [`architecture-framework.md`](./architecture-framework.md) §2 (*operational vs analytical persistence*), [`provider-spi.md`](../_plugins/provider-spi.md) §5 (*Memory Export Sink SPI*), [`event-hooks.md`](./event-hooks.md) (*wiring from hooks*), [`memory-portability.md`](./memory-portability.md) (*AMA export vs sink events*), [`data-governance.md`](./data-governance.md) (*classification follows content into sinks only when explicitly allowed*).

---

## 1. Design principles

1. **Orthogonal to memory tiers** — Sinks run **after** policy succeeds on the operational path (or on explicit export), like webhooks in [`event-hooks.md`](./event-hooks.md). They are **not** a third memory tier: they do not participate in `provider_tier: storage` vs `engine` negotiation.
2. **Event-oriented, not retrieval-shaped** — The protocol is **`emit(event)`** / optional **`flush()`**, not `search_similar()`. Vector search in Snowflake/BigQuery belongs in a **Tier 1 adapter** if you want the built-in pipeline to call it; this sink is for **durable analytics rows/files**.
3. **At-least-once by default** — Implementations should accept **idempotent keys** (`event_id`, `(bank_id, sequence)`, pattern) so warehouses and Iceberg merges dedupe cleanly.
4. **Governance-aware** — Payloads should respect redaction and classification: sinks **must not** widen exposure beyond what [`data-governance.md`](./data-governance.md) and policy allow (embeddings and raw text often **off** by default).
5. **Portable contract** — Same SPI shape in **Python and Rust** implementations of the framework (`implementation-language-strategy.md`).

---

## 2. When to use which integration

| Goal | Recommended approach |
|------|---------------------|
| Agent-time hybrid recall with warehouse-native vector SQL, lake query engine, or OLAP/BFF façade | **Tier 1** `VectorStore` / `GraphStore` / `DocumentStore` adapter against **that** query/search API—not the sink. |
| Long-term tables, BI, training exports, compliance timelines | **Memory export sink** (this document) + optional ETL from the sink’s staging layout. |
| **Both** analytics export and online recall against data that ultimately lives in the lake/warehouse | **Memory export sink** **and** a separate **Tier 1** integration; shared storage is optional—shared **contracts** for governance are not. |
| One vendor owns the full pipeline and storage | **Tier 2** `EngineProvider`; the engine may use a lakehouse internally. |
| One-off migration between providers | **AMA** export/import ([`memory-portability.md`](./memory-portability.md)), not necessarily a running sink. |

---

## 3. Event taxonomy (normative sketch)

Sinks consume a **versioned discriminated union** of events. The exact names belong in shared DTOs (`astrocytes.types`); this list is the **design contract**.

| Event kind (illustrative) | When emitted | Typical payload fields |
|---------------------------|-------------|------------------------|
| `RetainCommitted` | After retain succeeds | `event_id`, `bank_id`, `principal`, `memory_id`, `occurred_at`, `fact_type`, `tags`, content hash or **redacted** snippet length, classification labels |
| `ForgetApplied` | After forget succeeds | `event_id`, `bank_id`, `memory_ids`, `reason` |
| `RecallObserved` (optional) | After recall (sampled or config-gated) | aggregates only: `result_count`, `latency_ms`, `top_score` — avoid full queries |
| `ReflectObserved` (optional) | After reflect | similar aggregate discipline |
| `BankLifecycle` | Bank created/deleted | `bank_id`, action |
| `ExportCompleted` | After AMA or snapshot export | path URI, `memory_count`, checksum |
| `GovernanceSignal` (optional) | PII action, rate limit, hold | reference to policy codes — **no raw secrets** |

Implementations may **filter** event kinds via sink `capabilities()` or config.

---

## 4. Physical targets

Packages implement the same SPI against:

- **Object storage + table catalog** — Iceberg/Delta/Hudi/Paimon tables; Parquet as file format.
- **Warehouse load** — Snowpipe-style ingest, BigQuery `LOAD`, Redshift `COPY`, JDBC batch insert into curated tables.
- **Streaming bus** — Kafka/Pulsar topic consumed by Flink/Spark into the lake; the Astrocytes package may only publish to the bus.

The framework **does not** mandate one catalog or SQL dialect; adapters own connection config (Hive metastore, Glue, Unity, Polaris, …).

---

## 5. Configuration (illustrative YAML)

```yaml
memory_export_sinks:
  - provider: iceberg
    config:
      catalog_uri: thrift://metastore:9083
      database: astrocytes
      table: memory_events
      # embeddings: false   # default: do not replicate vectors to lake
```

Multiple sinks are allowed (for example staging + compliance archive).

---

## 6. Implementation status

The **Memory Export Sink** SPI is a **documented framework extension**. Core `astrocytes-py` / `astrocytes-rs` wiring (discovery, hook integration, conformance tests) may land incrementally; until then, deployers can use **`event-hooks.md`** webhooks toward custom ingestors that honor this event shape.

---

## 7. Naming and packaging

Community packages SHOULD use the prefix **`astrocytes-sink-`** (for example `astrocytes-sink-iceberg`, `astrocytes-sink-kafka`) — see [`ecosystem-and-packaging.md`](../_plugins/ecosystem-and-packaging.md).

Register implementations under the entry point group **`astrocytes.memory_export_sinks`** (specified alongside other groups in that document).
