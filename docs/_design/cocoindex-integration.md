# CocoIndex integration roadmap

**Status:** Proposed. Last updated 2026-05-04.

This document proposes how Astrocyte composes with [CocoIndex](https://github.com/cocoindex-io/cocoindex) — an open-source, declarative data-indexing framework with a Rust core and Python API — and how Astrocyte adopts a small set of CocoIndex's design patterns into its own pipeline. CocoIndex and Astrocyte solve **different problems** and are **complementary**, not competitive.

For the broader ecosystem positioning, see [`platform-positioning.md`](platform-positioning.md). For the agent-memory competitive landscape (Hindsight, Zep, Mem0, MemMachine), see [`hindsight-informed-capabilities.md`](hindsight-informed-capabilities.md) and [`benchmark-roadmap.md`](benchmark-roadmap.md).

---

## 1. The boundary: build-side vs read-side

The simplest way to remember which is which:

| Concern | CocoIndex | Astrocyte |
|---|---|---|
| Side of the loop | **Build / write** | **Read + write (agent-driven)** |
| Trigger | Source-of-truth changes (file modified, Slack message posted, S3 object added) | Agent calls `retain` / `recall` / `reflect` |
| Caller | Indexer process / cron / file watcher | Agent runtime |
| State semantics | Reconciled — old rows retired when source disappears | Append-mostly — `forgotten_at` for compliance |
| Time orientation | Past-only (indexes what currently exists) | Bidirectional — `as_of`, `occurred_at`, time-travel queries |
| Programming model | Declarative targets — describe desired state, engine reconciles | API-driven (REST + SDKs + MIP routing) |

```
┌──────────────────────────────┐
│        Data sources          │
│  (PDFs, Slack, GitHub, S3)   │
└──────────────┬───────────────┘
               │  index, transform, keep fresh
               ▼
        ┌─────────────┐
        │  CocoIndex  │   build side
        └──────┬──────┘
               │ writes to vector store / KG
               ▼
   ┌───────────────────────────┐
   │  Vector / Graph / Doc DB  │   ← shared substrate
   │  (Postgres + pgvector +   │
   │   AGE; Qdrant; Neo4j)     │
   └───────────┬───────────────┘
               │ retain / recall / reflect
               ▼
        ┌──────────────┐
        │  Astrocyte   │   read side
        └──────────────┘
                       │
                       ▼
                 ┌──────────┐
                 │  Agents  │
                 └──────────┘
```

Both projects write to the **same underlying substrate** (Postgres + pgvector + AGE in Astrocyte's reference stack; Qdrant or Neo4j in many CocoIndex examples). That's the integration seam.

---

## 2. Why Astrocyte cannot just become CocoIndex

Three architectural constraints make full CocoIndex semantics a poor fit for Astrocyte's domain:

1. **Memory writes are imperative, not declarative.** CocoIndex's premise is that the user *declares* the final database state ("this folder of PDFs should be indexed at this configuration"); the engine reconciles. Agents call `brain.retain(text=...)` ad-hoc — there's no static "this is what the bank should look like" declaration to diff against.
2. **Tombstone-on-source-disappearance is the wrong policy for memory.** CocoIndex retires rows when the upstream source vanishes. Astrocyte memories are explicitly retained even when the original conversation/document is gone — the whole point of `forgotten_at` and legal-hold is that retention is a separate decision from source presence.
3. **Bidirectional time queries don't fit a build-side reconciler.** Astrocyte's `as_of` time-travel and `occurred_at`-anchored retrieval require the storage to remember *when memories were retained, when events occurred, and when memories were forgotten* — three timestamps that have no analog in CocoIndex's "current desired state" model.

CocoIndex is correct for its problem domain. Memory is a different problem domain. The integration story is **composition**, not absorption.

---

## 3. The roadmap

Four phases, ordered by dependency and value-per-effort. Each phase stands alone — early phases ship without committing to later ones.

### Phase 0 — Today: parallel writers to the same Postgres

**Status:** Already works; needs documentation, not code.

A user can already point CocoIndex's `postgres` connector at the same Postgres instance Astrocyte's `astrocyte-postgres` adapter writes to, **with no code changes in either project**:

- CocoIndex writes its indexed embeddings into one schema (e.g. `cocoindex_engdocs`).
- Astrocyte writes agent memories into another schema (e.g. `public` or per-tenant via [schema-per-tenant](../plugins/schema-per-tenant/)).
- Astrocyte's recall reaches across schemas only when explicitly configured to do so — by default they are isolated.

**Deliverable:**
- A how-to under [`_end-user/`](../_end-user/) titled `cocoindex-as-source.md` showing the minimal docker-compose snippet, the `astrocyte.yaml` config that exposes the CocoIndex schema as a separate `bank_id`, and the recall semantics.

**Effort:** S (docs only).

### Phase 1 — `astrocyte-cocoindex` target connector

**Status:** Proposed. New package under `adapters-integration-py/`.

Build a CocoIndex **target connector** that calls Astrocyte's `/v1/retain` for each indexed unit, instead of writing directly to Postgres. This means CocoIndex users get Astrocyte's full intelligence pipeline — embedding, structured fact extraction, entity resolution, MIP-routed banks, lifecycle controls, observability — with one declaration in their CocoIndex app:

```python
# user's CocoIndex app
@coco.fn(memo=True)
async def index_engdocs():
    files = coco.use_context(ENGDOCS_FOLDER)
    coco.mount_each(
        ingest_one,
        files,
        target=coco.targets.astrocyte(
            url="http://astrocyte:8080",
            bank_id="engdocs",
            api_key=ENV["ASTROCYTE_API_KEY"],
        ),
    )
```

Astrocyte's contract is unchanged; the connector is a thin HTTP client on the CocoIndex side. Since CocoIndex memoizes by `hash(input) + hash(code)`, this also gives CocoIndex users the "rechunk without refetch" capability — re-running the app after editing the chunker only re-`retain`s changed chunks.

**Reciprocity:** the connector is contributed upstream to CocoIndex (under their `python/cocoindex/connectors/astrocyte/` convention), not housed in the Astrocyte monorepo. Astrocyte ships the matching gateway endpoint and authentication contract.

**Why this is the strategic phase:** it makes Astrocyte the **default agent-memory sink** for CocoIndex users without forcing them to re-architect anything. The friction of "set up a memory system separately" goes away.

**Effort:** M.
**Owner:** TBD; co-design with CocoIndex maintainers.
**Reference:** the [CocoIndex target-connector skill](https://github.com/cocoindex-io/cocoindex/tree/main/.claude/skills/target-connector) documents the contract.

### Phase 2 — Adopt CocoIndex patterns inside Astrocyte's pipeline

**Status:** Proposed. Internal refactors; no new packages.

CocoIndex's design contains four primitives that Astrocyte's own retain/ingestion paths would benefit from. None of them are agent-memory-specific; all of them are general pipeline-DX wins.

| # | Pattern | CocoIndex source | Astrocyte target | Effort | Win |
|---|---|---|---|---|---|
| 1 | Per-component update stats (`adds / deletes / reprocesses / errors / unchanged`) | `python/cocoindex/_internal/update_stats.py` | New `astrocyte/pipeline/stats.py`; gateway `/stats` | S | DX |
| 2 | `LiveMapFeed` push-source pattern with backpressure | `python/cocoindex/_internal/live_component.py`; `connectors/localfs/_source.py:185-231` | Refactor `adapters-ingestion-py/*/source.py` (5 adapters share a duplicated poll-loop pattern) | M | Robustness |
| 3 | Stable component path for retain trace | `python/cocoindex/_internal/stable_path.py`; `inspect_api.py` | New `astrocyte/pipeline/trace.py` (`RetainTrace` symmetric to `RecallTrace`) | M | Debugability — directly attacks the rerank-fault diagnosis gap measured at 49% of bench failures |
| 4 | Persistent fingerprint memoization for retain (`@coco.fn(memo=True)`) | `python/cocoindex/_internal/memo_fingerprint.py`; `rust/core/src/state/db_schema.rs` | New `astrocyte/pipeline/memo.py`; new `astrocyte_pipeline_memo` Postgres table | L | Re-ingest without refetch — solves the explicit "rechunk after editing the strategy" gap |

Patterns #1 and #2 stand alone. Pattern #3 starts paying off immediately for debugging; full value waits on #4. Pattern #4 is the big bet — it gates the others' compounding value but is itself the largest piece of work.

**Sequencing recommendation:** ship #1 first (small, additive, no risk), then #2 (eliminates duplication across five adapters), then #3 (debugging payoff), then #4 once the first three are stable.

**Effort summary:** S + M + M + L over 4-6 milestones.

### Phase 3 — Astrocyte retain events as a CocoIndex source (optional)

**Status:** Speculative. Only worth pursuing if downstream demand surfaces.

The mirror of Phase 1: expose Astrocyte's retain stream as a CocoIndex `LiveComponent` source. A separate analytics/search app could then consume an agent's accumulated memories as a fresh, deduplicated, MIP-routed feed — useful for org-level dashboards or for cross-agent learning.

This is genuinely speculative. It requires:
- A stable retain-event stream (the [memory-export-sink](memory-export-sink.md) work has the right primitives).
- Per-tenant isolation that CocoIndex's source contract respects.
- A clear use case beyond "it would be cool" — currently absent.

**Defer until a real customer asks for it.**

---

## 4. What this is NOT

Several patterns from CocoIndex look interesting in isolation but should **not** be adopted into Astrocyte. Listed here so future readers don't relitigate the decision.

| Tempting CocoIndex feature | Why it doesn't fit Astrocyte |
|---|---|
| The "React for data engineering" declarative-target model | Memory writes are imperative by design; agents call `retain()` ad-hoc with no pre-declared end-state to reconcile against. |
| App + CLI as first-class artifacts (`coco update myapp.py`) | Astrocyte runs as a daemon or library; there is no "app file" to point a CLI at. |
| Tombstone-on-source-disappearance (`StablePathEntryKey::ChildComponentTombstone`) | Memory retention is decoupled from source presence — see §2. |
| LMDB-based state DB (`heed` in Rust) | Astrocyte already runs Postgres; adding a second on-disk store is gratuitous. The fingerprint memo (Phase 2 #4) lives in a Postgres table. |
| Petabyte-scale Rust ingest hot path | Astrocyte's measured bottleneck is **retrieval quality** (LoCoMo 0.515 vs Hindsight 0.92), not ingest throughput. Porting retain to Rust is a misaligned bet. |
| `TargetReconcileOutput.child_invalidation: "destructive" | "lossy"` schema evolution | Astrocyte's numbered migrations + lazy `_ensure_schema` pattern are sufficient at the current rate of schema change. |

---

## 5. Relationship to Hindsight (for clarity)

Hindsight is the agent-memory product Astrocyte directly competes with on benchmark accuracy (current gap: ~33-40pp on LoCoMo / LongMemEval canonical-LLM judge). The Hindsight gap-closure plan is tracked separately in [`hindsight-informed-capabilities.md`](hindsight-informed-capabilities.md) and the [benchmark roadmap](benchmark-roadmap.md).

CocoIndex is **not** a Hindsight alternative. It does not implement `retain` / `recall` / `reflect` semantics, has no agent-memory benchmarks, and would not pass any of the four diagnostic tests in [`platform-positioning.md`](platform-positioning.md). Composing CocoIndex with Astrocyte is orthogonal to the Hindsight gap.

A user could compose all three tiers:

```
CocoIndex          Astrocyte gateway        Agent runtime
─────────          ────────────────        ─────────────
codebase   ─┐                                
PDFs       ─┼─→ /v1/retain  ─→  Postgres ─→ /v1/recall  ─→  framework
Slack      ─┘                                                (LangGraph,
                                                              CrewAI,
                                                              MCP, …)
```

— with CocoIndex handling source freshness and Astrocyte handling agent-driven memory operations and intent-routed retrieval.

---

## 6. Decision points and open questions

1. **Phase 1 ownership:** does the CocoIndex target connector live in their repo (`python/cocoindex/connectors/astrocyte/`) or ours (`adapters-integration-py/astrocyte-cocoindex/`)? **Lean upstream** — connectors are CocoIndex's primary extension point; living in their tree means CocoIndex CI exercises it on every release.
2. **Phase 2 #4 (fingerprint memo) scope:** does it cover only deterministic transformations (chunking, embedding) or also LLM-driven steps (fact extraction, entity resolution)? LLM-step fingerprints require explicit `model_id + temperature=0` keys to be honest; without those they will produce false cache hits. **Decision:** start with deterministic-only; add LLM steps as a separate opt-in flag.
3. **Phase 3 customer demand:** track requests under issue label `cocoindex-source`. Three independent requests = build it. Otherwise defer indefinitely.

---

## 7. Tracking

- ADR for Phase 1 contract: TBD
- Phase 2 #1 (update stats) will be a small commit to `pipeline/orchestrator.py` and `pipeline/stats.py` (new) — does not need an ADR.
- Phase 2 #4 (fingerprint memo) crosses architectural boundaries (new persistence, new APIs, mode in retain orchestrator) and **does** need an ADR before any code lands.
