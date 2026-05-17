# Couchbase: Comparison and Integration Path

**Status:** Living document. Last updated 2026-05-17.

Couchbase 8.0 (released October 2025) added a Hyperscale Vector Index (HVI)
and published tutorials showing it as a storage backend for agent memory
(CrewAI `ShortTermMemory`, LangChain vector stores). This document covers
what Couchbase is actually building, where the stack is genuinely superior,
where comparisons are unfair, where Couchbase has real advantages, and how
Couchbase fits as a backend infrastructure option rather than a competitor.

---

## Layer Architecture: Why These Are Not Competing Systems

Couchbase, Astrocyte, Hindsight, and Mem0 operate at different layers.
Conflating them produces misleading comparisons.

```
┌────────────────────────────────────────────────────────────┐
│  Memory intelligence                                        │
│  (extraction, entity linking, dedup, reflection)           │
│  → Hindsight, Mem0                                         │
├────────────────────────────────────────────────────────────┤
│  Memory governance                                          │
│  (policy, auth, multi-tenant routing, lifecycle)           │
│  → Astrocyte                                               │
├────────────────────────────────────────────────────────────┤
│  Vector storage infrastructure                              │
│  (indexing, ANN search, persistence at scale)              │
│  → Couchbase HVI, pgvectorscale, Qdrant, Pinecone, etc.   │
└────────────────────────────────────────────────────────────┘
```

**The correct read of Couchbase's memory tutorial:** it is a storage backend
demo, not a memory framework. It shows how to keep raw agent interaction
strings in a Couchbase collection and retrieve them by cosine similarity.
That is the storage tier. Hindsight and Mem0 solve the intelligence tier
above it; Astrocyte solves the governance tier above that.

Astrocyte can *run on* Couchbase as a vector backend — see the integration
path section below.

---

## What Couchbase's Hyperscale Vector Index Actually Does

HVI uses **Inverted File (IVF) clustering + scalar/product quantization**:

- Vectors are partitioned into centroid clusters (default: total_vectors ÷ 1000)
- At query time, a configurable number of cluster probes are scanned
  (`probes`, default: 1) via `APPROX_VECTOR_DISTANCE()`
- Vectors are stored in quantized form (SQ4/SQ6/SQ8 or PQ subspaces),
  reducing memory footprint at the cost of precision

The tutorial stack: OpenAI `text-embedding-3-small` → Couchbase collection
(JSON document + vector field) → cosine similarity retrieval with optional
type-based metadata filter.

**No memory intelligence runs in Couchbase.** Raw agent output strings are
stored directly. The tutorial patches around this in application code
(stripping "Final Answer:" prefixes before storage). Entity extraction,
deduplication, temporal reasoning, and synthesis do not exist in this model.

---

## Where We Are Genuinely Superior

### 1. Memory intelligence — a different category entirely

Hindsight's `retain()` and Mem0's `add()` run LLM-driven extraction before
anything hits storage: fact normalization, entity linking and deduplication,
causal chain extraction, temporal metadata. What gets stored is structured
knowledge, not raw strings.

Couchbase stores what you give it. The intelligence lives entirely in
application code, and the Couchbase tutorial does not provide it.

### 2. Index algorithm: DiskANN vs IVF+quantization

Astrocyte's Postgres adapter uses **pgvectorscale (DiskANN)** exclusively —
standardized in 2026-05 after LongMemEval benchmarking exposed HNSW
write-lock drift (1.0s → 2.0s per session under concurrent retain).
Hindsight also ships DiskANN as a first-class option.

These are different algorithm families with different properties:

| Property | Couchbase HVI (IVF+SQ8) | pgvectorscale (DiskANN) |
|---|---|---|
| Algorithm family | Cluster-based (IVF) | Graph-based |
| Vector precision | Quantized — lossy | Full precision on SSD |
| Recall at high QPS | ~66% (Couchbase's own claim) | ~95%+ (Timescale benchmarks) |
| Write-lock behavior | N/A | Serializes less aggressively than HNSW under concurrent writes |
| Query composition | Separate from DB | In-Postgres: DiskANN + BM25 + graph (AGE) in one query |

The 66% recall is Couchbase's peak-throughput figure from their own billion-
scale benchmark. At the millions-of-memories scale relevant to per-user or
per-agent memory stores (not billions), DiskANN graph traversal consistently
out-recalls cluster-based IVF at equivalent latency.

**Important caveat on the numbers:** Couchbase's 66% measures *ANN recall* —
did the index return the true nearest neighbors? LongMemEval / LoCoMo measure
*memory quality* — did the system return the right fact for a question? These
are different metrics and cannot be directly compared. See
[`benchmark-comparison-methodology.md`](benchmark-comparison-methodology.md)
for rules on cross-system number comparisons.

### 3. Multi-signal retrieval

Hindsight fuses four retrieval signals via reciprocal rank fusion (RRF), then
cross-encoder reranks:

1. Dense semantic similarity (DiskANN/pgvectorscale)
2. Sparse BM25 keyword (PostgreSQL full-text search)
3. Graph entity expansion (entity links, causal chains)
4. Temporal range filtering

Couchbase's retrieval is single-signal: cosine ANN only. Replicating the
multi-signal pipeline requires heavy application-layer stitching with no
in-database support for BM25 or graph traversal.

### 4. Reflection and synthesis

Hindsight's `reflect()` and Mem0's equivalent synthesize higher-order
insights from stored memories via an LLM distillation pass. Couchbase has
no analog — it is purely a retrieval system.

### 5. Lifecycle and governance

Astrocyte provides soft/hard/tombstone forget modes, legal hold, min-age-days
enforcement, cascade semantics, `forgotten_at` timestamps, per-bank access
grants (read/write/forget/admin), and compliance profiles (GDPR/HIPAA/PDPA).
These do not exist in the Couchbase model and would require full application-
layer implementation.

---

## Where Couchbase Has Legitimate Advantages

Be honest about these rather than dismissing them.

**Billion-scale indexing.** Couchbase has published evidence of IVF+SQ8
performing at 1B+ vectors. pgvectorscale scales to the hundreds-of-millions
range and is improving, but Couchbase owns the "we tested this at 1B vectors"
story today. This matters for a platform-level shared index across tens of
millions of users — less relevant for per-user or per-agent scopes.

**Existing Couchbase customers.** Zero-migration path for enterprises already
running Couchbase. A go-to-market advantage, not a technical one, but real.

**Edge/mobile deployment.** Couchbase Lite enables on-device persistent
vector stores. None of the three repos ship an on-device embedded vector
index story at parity.

**Tunable recall/latency per query.** The `probes` parameter lets callers
trade accuracy for speed at query time. Useful for latency-sensitive
pipelines that can tolerate lower recall on some queries.

---

## The Simplicity Trap

Couchbase's real competitive risk is not technical — it is the simplicity trap:
developers who reach for the database they already have, build a
store-and-search pipeline, accept lower memory quality, and never measure it.

The counter is making memory quality metrics visible and concrete so teams
can make an informed tradeoff. The four diagnostic tests in
[`platform-positioning.md`](platform-positioning.md) are the framework for
this conversation: gap analysis, entity resolution, time travel, sovereignty.
A Couchbase-backed store-and-search pipeline fails all four.

---

## Couchbase as Backend Infrastructure

Couchbase is a legitimate storage backend option, not only a competitor.
Astrocyte's Tier 1 VectorStore SPI is backend-agnostic by design — the
contract is `embed`, `store`, `search`, `delete`. A Couchbase adapter would
sit alongside `astrocyte-postgres` as a deployment target.

**When a Couchbase adapter makes sense:**

- Customer is already running Couchbase at scale and does not want to
  introduce Postgres
- Workload genuinely exceeds the hundreds-of-millions range where
  pgvectorscale is well-tested
- Edge deployment with Couchbase Lite (future work)

**What a Couchbase adapter would and would not provide:**

| Capability | With Couchbase adapter |
|---|---|
| Memory intelligence (extraction, entity linking) | Yes — handled by Astrocyte pipeline above the storage layer |
| Governance (per-bank auth, lifecycle, compliance) | Yes — handled by Astrocyte above the storage layer |
| Multi-signal retrieval (BM25, graph) | Partial — Couchbase lacks native BM25 and graph; these signals would degrade or require separate services |
| DiskANN recall quality | No — Couchbase uses IVF+quantization; a Couchbase adapter would accept the lower recall tier |
| Billion-scale capacity | Yes — Couchbase's IVF handles this; pgvectorscale does not |

**Current status:** No Couchbase adapter exists. The adapter package pattern
is documented in [`adapter-packages.md`](adapter-packages.md). The SPI is in
`docs/_plugins/provider-spi.md`.

**Recommended framing when a prospect raises Couchbase:**

> Couchbase is a valid vector storage backend. Astrocyte can run on it.
> The question is which storage backend gives you the recall quality and
> query expressiveness your memory workload needs. For most per-user or
> per-agent memory scopes, DiskANN in Postgres gives better recall at lower
> TCO. For workloads that genuinely exceed hundreds of millions of vectors,
> or for teams already on Couchbase, the adapter path exists.

---

## Summary

| Dimension | Us | Couchbase |
|---|---|---|
| **Category** | Memory intelligence + governance | Storage infrastructure |
| **Index algorithm** | DiskANN (graph, full-precision) | IVF+quantization (cluster, lossy) |
| **Retrieval signals** | Semantic + BM25 + graph + temporal (RRF) | Cosine ANN only |
| **Memory intelligence** | LLM extraction, entity linking, dedup, reflection | None |
| **Governance** | Per-bank auth, lifecycle, compliance, MIP routing | None |
| **Write concurrency** | DiskANN: low write-lock drift | Not benchmarked |
| **Scale ceiling** | ~100M–1B (pgvectorscale, improving) | 1B+ (published) |
| **Edge/mobile** | Not shipped | Couchbase Lite |
| **Backend relationship** | Can run *on* Couchbase (adapter needed) | Can serve as Astrocyte Tier 1 backend |
