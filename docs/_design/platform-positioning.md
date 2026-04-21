# Astrocyte + Mystique: Platform Positioning

**Status:** Living document. Last updated 2026-04-18.

This document maps Astrocyte (open-source memory framework) and Mystique
(commercial control plane) onto the enterprise agentic platform reference
architecture — five layers that enterprises will build regardless, of which
memory / context is the layer with the highest strategic value.

Reference: mdjawad, *Enterprise Agentic Platform* (Apr 2026).

---

## The Five Layers, Restated

| Layer | Concern |
|-------|---------|
| **L1 — Capability Surface** | What tools/MCP servers does the agent see? How are they progressively disclosed? |
| **L2 — Identity & Policy** | Who is acting (human + agent identity)? What are they allowed to do? Scoped short-lived tokens, async approval. |
| **L3 — Context Pipeline** | Ingestion → indexing → governed retrieval. The "context is the moat" layer. |
| **L4 — Evaluation Harness** | Golden sets, LLM-as-judge, shadow traffic, CI gates, production feedback → regression tests. |
| **L5 — FinOps & Observability** | Per-team / per-task / per-session cost attribution and routing. |

The article's thesis: **context is the moat, not the model**. L3 is the
strategic layer; L1, L2, L4, L5 are the supporting infrastructure that makes
L3 governable, attributable, and improvable.

---

## Astrocyte's Position

Astrocyte is the **governed retained-memory slice of L3**, with deliberate
integration surface into L1, L2, L4, L5. It is the open-source substrate;
it does not aspire to own the whole platform.

| Layer | Astrocyte's role | Depth |
|-------|------------------|-------|
| L1 | MCP server (`astrocyte-mcp`) exposes memory as a capability. MIP's declarative rules are the memory-path equivalent of progressive disclosure — route by intent before loading full retrieval pipelines. | **Integrated component** |
| L2 | Per-bank access grants (read/write/forget/admin), compliance profiles (PDPA/GDPR/HIPAA), audit trails. Consumes upstream identity decisions; does not issue tokens. | **Integrated component** |
| L3 | **Self-contained and complete** for the *retained* slice (session-scoped agent memory, consolidation over time, multi-strategy retrieval). Does not own the *source-of-truth ingestion* slice (Confluence, GitHub, Slack crawlers). | **Self-contained** |
| L4 | Reflect + OpenTelemetry traces give the primitives. LoCoMo / LongMemEval runners exist. Opinionated eval harness (CI gate, golden-set management, thumbs-down-to-regression loop) is the **one honest gap**. | **Primitives present, harness in-flight** |
| L5 | OpenTelemetry spans + Prometheus metrics per operation. Rate limits + token budgets on the retain/recall path. Feeds the upstream LLM gateway; does not own cost attribution. | **Integrated component** |

### The positioning line

> Astrocyte is the governed memory layer of the enterprise agentic platform.

Not "memory for agents." Not "RAG library." **Governed memory as
strategic infrastructure** — the layer enterprises will build regardless,
purpose-built for the job.

### Where Astrocyte is *ahead* of the article's reference architecture

1. **Declarative routing (MIP).** The article argues for governance at the
   pipeline level but offers no mechanism more opinionated than "policy-as-code
   at the gateway." MIP is a full DSL — presets, tie-breaking, shadow mode,
   time-bounded activation, observability tags.
2. **Lifecycle as first-class.** Soft/hard/tombstone forget modes, legal
   hold, min-age-days enforcement, cascade semantics. The article acknowledges
   governance but doesn't specify mechanism at this level of detail.
3. **Pluggable provider tiers.** Tier 1 (built-in pipeline) vs. Tier 2
   (full-stack engines like Mystique, Mem0, Zep). The article assumes a
   single serving layer; Astrocyte's SPI makes substrate choice orthogonal
   to the memory contract.

---

## Mystique's Position: The Commercial Control Plane

Mystique is the opinionated commercial offering that **deepens Astrocyte's
capabilities** (as a Tier 2 engine) and **closes the platform gaps** Astrocyte
leaves open by design (as the control plane UI and managed infrastructure
around the memory substrate).

### Mystique as Tier 2 Engine (deepens L3)

- Managed, production-grade storage (Postgres + pgvector, HNSW, Apache AGE
  for graph, content-hash delta retain).
- Opinionated pipeline: 4-way parallel retrieval (semantic + BM25 + graph
  spreading-activation + temporal) fused with RRF.
- Write-time consolidation: background LLM job distills raw facts into
  higher-order observations (Hindsight-style).
- Entity extraction with gleaning passes (EdgeQuake-style multi-pass).
- Bank disposition traits (skepticism, literalism, empathy) that shape
  extraction/reasoning behavior.

### Mystique as Control Plane (closes L1, L2, L4, L5)

| Layer | Mystique closes |
|-------|-----------------|
| **L1** | MCP gateway UI: discover + install + scope connectors (Confluence, GitHub, Slack, Jira). Progressive disclosure rules editor — which capabilities surface for which agents. |
| **L2** | Identity UI: human + agent principals, scoped-token issuance, async approval queues, policy-as-code editor, compliance mode dashboards. |
| **L3 (ingestion)** | Source-of-truth connectors that Astrocyte deliberately doesn't own — crawl, normalize, hand off to Astrocyte banks with the right MIP rules pre-wired. |
| **L4** | Eval harness as a product: managed golden sets, LLM-as-judge panels, shadow traffic replay, CI gate for PR regressions, production thumbs-down → regression test loop. This is the "eval chapter of the article," productized. |
| **L5** | FinOps dashboard: per-team / per-repo / per-task / per-session cost attribution. Cost-per-merged-PR, cost-per-ticket-resolved. LLM gateway integration with tiered routing. |

### The Mystique thesis in one line

> Astrocyte is the substrate. Mystique is the platform around it — the
> control plane UI, the managed integrations, the eval infrastructure, and
> the FinOps surface that turn governed memory into an enterprise product.

Enterprises who adopt Astrocyte alone get: the memory substrate, direct code
integration, operator CLI (`astrocyte mip lint|explain`), full SDK control.

Enterprises who adopt Astrocyte + Mystique get: everything above **plus** a
control plane UI, managed ingestion, productized eval, FinOps attribution,
and the 4/5 non-memory platform layers without building them in-house.

---

## Layered Deployment Picture

```
┌─────────────────────────────────────────────────────────────┐
│  L5  FinOps / LLM Gateway / Observability                   │
│      ▲ Astrocyte: otel spans, prom metrics, token budgets   │
│      ▲ Mystique:  cost attribution UI, tiered routing       │
├─────────────────────────────────────────────────────────────┤
│  L4  Evaluation Harness                                     │
│      ▲ Astrocyte: reflect, LoCoMo/LongMemEval runners       │
│      ▲ Mystique:  golden sets, CI gates, shadow replay      │
├─────────────────────────────────────────────────────────────┤
│  L3  Context Pipeline                                       │
│      ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━│
│      ┃  Astrocyte  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ ┃│
│      ┃  Retained memory: MIP routing, chunk/embed/store,   ┃│
│      ┃  multi-strategy recall, reflect, lifecycle, PII     ┃│
│      ┃  barriers, forget modes                             ┃│
│      ┃                                                     ┃│
│      ┃  Mystique Tier 2 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┃│
│      ┃  Managed Postgres + AGE, 4-way fusion, consolidation,┃│
│      ┃  gleaning, bank dispositions                        ┃│
│      ┃                                                     ┃│
│      ┃  ▲ Mystique: ingestion connectors (Confluence,      ┃│
│      ┃               GitHub, Slack, Jira)                  ┃│
├─────────────────────────────────────────────────────────────┤
│  L2  Identity & Policy                                      │
│      ▲ Astrocyte: per-bank grants, compliance profiles     │
│      ▲ Mystique:  scoped tokens, async approval, audit UI  │
├─────────────────────────────────────────────────────────────┤
│  L1  Capability Surface (MCP)                               │
│      ▲ Astrocyte: astrocyte-mcp (memory as capability)      │
│      ▲ Mystique:  MCP gateway UI, connector marketplace     │
└─────────────────────────────────────────────────────────────┘
```

Legend: `▲` = integration point / contribution. `━━` = owned surface.

---

## Consequences for Product Strategy

1. **Astrocyte's open-source scope is fixed by this mapping.**
   L3-retained-memory + documented integration surfaces into L1/L2/L4/L5.
   Ingestion, FinOps dashboards, connector marketplaces, eval-as-CI — these
   are Mystique concerns, not Astrocyte concerns.

2. **L4 is the single biggest open-source investment opportunity.**
   The article says "the eval harness is the chapter enterprises underbuild."
   Astrocyte has the primitives. Benchmarks (LoCoMo, LongMemEval) are the
   first golden sets. Making eval reproducible, CI-gateable, and regression-
   aware is the work that most directly validates the platform thesis.

3. **Every pattern we borrow from Hindsight / EdgeQuake should land in
   Astrocyte first (as SPI or optional stage), then deepen in Mystique.**
   Example: RRF fusion and disposition traits are Astrocyte-level. The
   managed-consolidation-job UI is Mystique-level.

4. **Mystique's control plane UI is not just "Astrocyte with a GUI."**
   It is the platform wrapper that makes the non-memory layers (L1/L2/L4/L5)
   operational for enterprises without them building those layers in-house.

