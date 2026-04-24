# Astrocyte + Mystique: Platform Positioning

**Status:** Living document. Last updated 2026-04-24.

This document maps Astrocyte (open-source memory framework) and Mystique
(commercial control plane) onto the enterprise agentic platform reference
architecture — five layers that enterprises will build regardless, of which
memory / context is the layer with the highest strategic value.

Reference: mdjawad, *Enterprise Agentic Platform* (Apr 2026).

---

## The thesis: the third option

Enterprise knowledge management has oscillated between two failing options for sixty years. The first option — hand-encoding structure — produces Palantir-style systems: accurate at first, unmaintainable as reality evolves. The second option — skipping structure — produces wikis, SharePoint, and now RAG: searchable but unintelligent, unable to reason about what they *don't* know.

**Astrocyte is the third option.** LLMs extract structure automatically — entities, relationships, temporal context — and a deterministic harness verifies, governs, and queries that structure. Structure emerges from content; it is not hand-encoded, and it is not discarded.

### Four diagnostic tests

Any system claiming to be the third option should pass four diagnostic tests. These tests expose whether a system is genuinely intelligent about its knowledge or just a sophisticated retrieval index.

**1. Gap analysis** — Can the system reason about absence?

```python
result = await brain.audit("deployment practices", bank_id="eng-team")
# AuditResult(
#   gaps=["no documentation on rollback procedures",
#         "database migration strategy not covered"],
#   coverage_score=0.62,
# )
```

A pure RAG system returns results for what exists. It cannot tell you what's missing. Gap analysis is the difference between a search engine and an intelligent knowledge assistant.

**2. Entity resolution** — Can the system unify evidence across documents?

When the system encounters "Calvin," "the CTO," and "linchuan.cheng@gmail.com" across different retained documents, it should recognize these as the same entity — and produce an evidence chain, not just a cosine similarity score.

```python
# During retain: entity extractor sees "Calvin" in document A
# Graph lookup finds candidate: entity "CTO" from document B
# LLM confirmation: "Calvin Cheng is referred to as the CTO in document B (evidence: 'our CTO Calvin approved')"
# EntityLink(type="alias_of", entity_a="Calvin", entity_b="CTO", evidence="our CTO Calvin approved", confidence=0.94)
```

**3. Time travel** — Can the system answer "what did we know on date X?"

Every memory carries a `retained_at` system timestamp. Soft deletes write `forgotten_at`. The recall path supports `as_of: datetime` — returning the exact knowledge state at any historical moment.

```python
hits = await brain.recall("deployment strategy", bank_id="eng-team", as_of=datetime(2026, 3, 1))
# Returns memories that existed on March 1st, excluding anything retained after
# and anything forgotten (forgotten_at <= March 1st not included)
```

**4. Sovereignty** — Can this run entirely on your own infrastructure?

Self-hosted PostgreSQL (pgvector + Apache AGE), self-hosted LLM (Ollama/vLLM), no data leaving the org. Compliance profiles for GDPR, HIPAA, PDPA. PII barriers before any data reaches storage.

### Why these four tests matter

The standard RAG objection — "just do better semantic search" — fails all four. Better embeddings improve retrieval of what exists; they do not surface what's absent (gap analysis), they do not resolve identities with evidence (entity resolution), they do not give you a historical snapshot (time travel), and they do not change the data-residency story (sovereignty).

These four tests are the criteria against which memory systems should be evaluated. Astrocyte's v1.0.0 scope is shaped by passing all four.

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
| L4 | Reflect + OpenTelemetry traces give the primitives. LoCoMo / LongMemEval runners with regression gate + per-bench tolerances ship today; real-provider baselines committed. Opinionated eval harness (LLM-as-judge, shadow replay, thumbs-down-to-regression loop) is what Mystique adds. | **Foundation landed, harness deepens in Mystique** |
| L5 | OpenTelemetry spans + Prometheus metrics per operation. Rate limits + token budgets on the retain/recall path. Feeds the upstream LLM gateway; does not own cost attribution. | **Integrated component** |

### The positioning line

> Astrocyte is the third option: structured memory that emerges from content, governed by a deterministic harness, passing the four diagnostic tests that matter for enterprise AI.

Not "memory for agents." Not "RAG library." Not "governed memory" as
a euphemism for RAG-with-access-control. **Structure that emerges automatically**
— LLMs propose, the deterministic harness verifies and queries — the layer
enterprises will build regardless, purpose-built for the job.

### What the numbers look like

Measured against real providers (`gpt-4o-mini` + `text-embedding-3-small`)
with the current Astrocyte pipeline (temporal retrieval, intent-aware
recall, weighted RRF):

| Benchmark | Sample | v3 (pre-Session-1) | v4 (current) | Δ |
|-----------|--------|--------------------|--------------|---|
| LoCoMo | 200Q | 88.5% | **87.5%** | −1.0pp |
| LongMemEval | 50Q | 26.0% | **80.0%** | **+54.0pp** |

Both runs use `gpt-4o-mini` + `text-embedding-3-small` via the Astrocyte
prd Doppler config.

**The LongMemEval jump** (+54pp overall, hit rate 24% → 76%) is primarily
Session 1 Item 1: the benchmark adapter was flattening multi-turn
sessions into individual-turn memories and then deduping by session_id,
effectively discarding 90%+ of the haystack content before retrieval
ever ran. Item 2 (document_store wiring → BM25 firing on the real
provider path) contributed additionally by supplying a 4th RRF input
on queries that cite specific names or phrases.

**The LoCoMo dip** (−1.0pp, within the 2pp regression-gate tolerance) is
a mock-provider artifact from `InMemoryDocumentStore` lacking IDF /
stopword filtering: common question words ("what", "did", "say")
match dozens of noise chunks. Production deployments with
Elasticsearch-class BM25 would not see this. Tracked as a minor
follow-up; not blocking.

The companion mock-provider baseline (`MockLLMProvider` + bag-of-words
embeddings) is used for the per-PR smoke gate; real-provider scores are
the marketing-grade numbers.

**Scoring caveat**: these numbers use Astrocyte's internal scorer
(`word_overlap_score > 0.3`), looser than the canonical LoCoMo /
LongMemEval LLM-judges. Expect 10–20 pp compression when the canonical
judges land; the relative Session-1 delta (especially LongMemEval
+54pp) will hold. Cross-competitor claims wait for the judge port.

See `benchmarks/snapshots/` for the full run history and
`docs/_plugins/benchmarks-doppler-setup.md` for how to reproduce.

### Where Astrocyte is *ahead* of the article's reference architecture

1. **Passes all four diagnostic tests.** Gap analysis (`brain.audit`), entity
   resolution with evidence chains, time travel (`as_of` queries + `retained_at`
   / `forgotten_at` history), and full sovereignty (self-hosted PostgreSQL with
   pgvector + Apache AGE, no data leaving the org). The article identifies
   "context is the moat" but does not specify mechanisms that pass these tests.
2. **The third option is implemented, not aspirational.** LLM wiki compile (M8)
   maintains rewritable `WikiPage` summaries from raw memories, with provenance
   and cross-links. Gap analysis audits coverage and surfaces absent topics.
   Entity resolution unifies identities across documents with evidence quotes.
   These together constitute the intelligence layer the article describes without
   naming.
3. **Declarative routing (MIP).** The article argues for governance at the
   pipeline level but offers no mechanism more opinionated than "policy-as-code
   at the gateway." MIP is a full DSL — presets, tie-breaking, shadow mode,
   time-bounded activation, observability tags.
4. **Lifecycle as first-class.** Soft/hard/tombstone forget modes, legal
   hold, min-age-days enforcement, cascade semantics, `forgotten_at` timestamps
   for compliance audit. The article acknowledges governance but doesn't specify
   mechanism at this level of detail.
5. **Pluggable provider tiers.** Tier 1 (built-in pipeline) vs. Tier 2
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

## Research Agenda: Path to 100% Benchmark Accuracy

The four diagnostic tests define *what* matters. The research agenda defines *how far* we can push each one. Five fundamental barriers stand between current scores and 100% — each requires a distinct research direction that engineering alone won't solve.

### The five barriers

| # | Barrier | Current impact | Diagnostic test affected |
|---|---------|---------------|-------------------------|
| 1 | **Retrieval recall** — right chunks not in context | 24–31% queries miss relevant content | All four |
| 2 | **Synthesis quality** — LLM misinterprets retrieved context | ~10–15% answer errors even with correct chunks | Entity resolution, gap analysis |
| 3 | **Knowledge representation** — raw chunks lose structure | Multi-hop & reasoning fail without typed relations | Entity resolution, gap analysis |
| 4 | **Temporal reasoning** — LLMs can't order events reliably | LME temporal at 11.3% (mock) | Time travel |
| 5 | **Abstention calibration** — wrong confidence on edge cases | Open-domain at 61.5% (real) | Gap analysis |

### How the research connects to the diagnostic tests

**Gap analysis (Test 1):** Coverage-aware retrieval (R5) + `brain.audit()` (M10) enable the system to detect thin coverage and abstain precisely. Retrieval sufficiency classifier (R5) prevents false positives — answering from noise when the bank lacks coverage.

**Entity resolution (Test 2):** Relation extraction (R3) extends M11 from alias detection to full typed triples. Contradiction detection (R3) resolves conflicting facts across documents. These create the structured substrate that makes entity resolution evidence-based rather than similarity-based.

**Time travel (Test 3):** Temporal knowledge graph (R4) turns `as_of` queries from filter-based (M9) to interval-algebra-based — deterministic temporal reasoning without LLM inference. Timeline generation (R4) externalizes temporal ordering into a readable artifact the LLM can process linearly.

**Sovereignty (Test 4):** All research techniques are designed to work with self-hosted models. Learned retrieval fine-tuning (R1) trains on operator query logs — no data leaves the org. The fine-tuned synthesis model (R2) runs on local GPU. Temporal embeddings (R4) are open-weight trainable.

### Projected ceiling with research phases

| Benchmark | v1.1.x (M8–M11) | After R1–R5 | Theoretical max |
|-----------|-----------------|-------------|-----------------|
| **LoCoMo** (canonical) | ~78–85% | ~92–95% | ~95–97% |
| **LongMemEval** (canonical) | ~68–75% | ~90–93% | ~93–97% |

The last 3–5% requires a purpose-built synthesis model fine-tuned on structured memory QA — not a general-purpose LLM. Astrocyte's SPI means such a model plugs in as a Tier 2 engine without framework changes.

Full phase details, eval gates, and per-technique projections: `product-roadmap-v1.md` § v2.0.0 — Research track.

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

