# Storage and data planes

This page is a **routing hub**: it explains how Astrocyte splits **operational retrieval** (including recall against **serving / query layers** on top of warehouses and lakehouses), **durable export** to those systems for analytics, and **in-process bank health**—and which document to read next.

**Canonical architecture:** [`architecture.md`](./architecture.md) §2 (*operational retrieval vs analytical persistence*) and §4 (*SPI overview*).

---

## 1. Operational retrieval (agent-time `recall`)

**What:** Tier 1 **`VectorStore`** (required), optional **`GraphStore`**, optional **`DocumentStore`**. The built-in pipeline calls `search_similar`, graph neighborhood queries, and full-text search when configured.

**Includes:** pgvector, Pinecone, Qdrant, Neo4j, … **Also** a data warehouse or lake SQL engine **if** you implement those APIs as a Tier 1 adapter (vector SQL, acceptable latency for your SLOs).

**Not:** Raw Parquet/Iceberg files on object storage **as** the recall path unless something in your stack exposes a **query API** the adapter implements.

**Serving layers and query façades:** The Tier 1 contract is **`VectorStore` / `GraphStore` / `DocumentStore`**, not a brand name like “lakehouse.” In practice, **agent-time `recall` often hits a serving hop** on top of warehouse or lake data: native warehouse SQL + vector distance, a **lake query engine** (Trino, Spark SQL, DuckDB over cataloged tables), a **low-latency OLAP** tier fed from the lake, or a **thin HTTP Backend for Frontend (BFF)** your team owns that runs the right SQL or search calls. The adapter is written against **that** API—the façade is an implementation detail.

**Same store, two roles:** One organization may run **both** (1) a **Memory export sink** into Iceberg/BigQuery/Snowflake for governed **append / analytics**, and (2) a **Tier 1** adapter that issues **online** search queries against the **same** vendor or tables **only if** latency, indexing, and semantics meet your recall SLOs. Those are **separate** integrations: export does **not** imply recall, and recall does **not** replace export.

**Poor fit without adaptation:** Pure **aggregated metrics** or **BI semantic** surfaces that only expose KPIs—not row- or chunk-level evidence—do **not** map cleanly to the built-in hybrid retrieval pipeline unless you add a different retrieval substrate or a custom orchestration path.

**Read next:** [Provider SPI](/plugins/provider-spi/) §1, [Built-in pipeline](./built-in-pipeline.md).

---

## 2. Durable export (warehouse / lakehouse / open tables)

**What:** Optional **Memory export sink** SPI—**`emit(event)`** / **`flush()`** after policy succeeds. Append governed rows/events to Snowflake, BigQuery, Iceberg, Delta, Parquet layouts, Kafka → Flink, etc. This is **not** `search_similar`; it is **ingest** for BI, compliance, and ML datasets.

**Not:** Tier 2 memory engine lifecycle, and **not** the same as in-process **bank health** metrics (see §3).

**Read next:** [Memory export sink](./memory-export-sink.md), [Provider SPI](/plugins/provider-spi/) §5, [Event hooks](./event-hooks.md) §1.1 (webhook bridge until core wiring), [Ecosystem & packaging](/plugins/ecosystem-and-packaging/) §2.6 and §3.5.

---

## 3. In-process bank health and utilization

**What:** Metrics inside Astrocyte—health scores, utilization, Prometheus—documented as **memory analytics** historically; sidebar label **Bank health & utilization**.

**Not:** Warehouse fact tables. For external analytical tables, use §2.

**Read next:** [`memory-analytics.md`](./memory-analytics.md).

---

## 4. Portability and snapshots

**AMA** export/import for migration between providers; overlaps **export** events only where you explicitly snapshot. See [`memory-portability.md`](./memory-portability.md).
