---
title: "SOTA roadmap — M45-M48 (answerer matrix → retrieval residue → matched harness → reasoning/MemoryArena)"
draft: false
topic: design
---

# SOTA roadmap — M45-M48

**Status:** PROPOSED (2026-09-01; revised same day after the AI-memory landscape survey — see §0b)
**Predecessor:** v0.15.1 (cycle `v015w` ship-floor: LME 74.4% @ mt_8192 n=90; LoCoMo 84.5% n=200 / 82.1% n=1540)
**Goal:** credible SOTA positioning across the matched-harness leaderboards (AML, LongMemEval-V2, AMA-Bench, MemoryArena), with an explicit cost/latency/accuracy tiering doctrine.
**Hard date:** AML submission cycle 2 opens **2026-09-20** (§4).

## 0. Inputs

**External research series (2026-09-01), verdicts:**

| Source | Verdict | Bench impact |
|---|---|---|
| OKF v0.2 (GoogleCloudPlatform/open-knowledge-format) | Format only, zero retrieval mechanism. Wiki tier already exceeds it except `stale_after` / `status` / trust tiers. | None |
| Atlan memory-vs-RAG-vs-KG | Vendor taxonomy; we already occupy the "all three, composed" quadrant. | None |
| OpenMetadata 2.0 "Context Memories" | Orthogonal (data-catalog memory). One borrowable pattern: MCP composed-context tool shapes. | None |
| Khullar "OKF+RAG" | Strictly weaker restatement of our wiki-first + answer-time routing; up-front hard router = the twice-failed shape. | None-to-negative |

Conclusion: **no external source moves benchmarks; the SOTA path is internal.** External value routes to the ecosystem lane (§6).

## 0b. Landscape update (2026-09-01 survey) — findings that changed this roadmap

1. **The Agent Memory Leaderboard (AML) exists** (launched 2026-07-29 by 20+ institutions; first results 2026-08-12). Matched harness: systems expose only Add/Search; the platform controls answerer, judge, scoring; explicit anti-gaming rules. **Top score across 69 frameworks: 58.02** (MemoraX, commercial); MemOS 45.89 (vs its own 88-89 self-reports — a 43-point self-report-to-matched gap on public display); best OSS 45.06 (InvMem). **Cycle 2 opens 2026-09-20.** This supersedes the Mem0-harness plan as the credibility centerpiece (§4). Sources: github.com/AML-memory/agent-memory-leaderboard, agentmemoryleaderboard.ai.
2. **LoCoMo is formally discredited as a precision instrument** (Penfield Labs audit, 2026-04-04): 6.4% of the answer key is wrong; the gpt-4o-mini judge accepts **62.8% of intentionally wrong answers**; theoretical max ≈ 93.6%. The 92-94% vendor cluster (Mem0 92.5, EverMemOS 92.3, TrueMemory 93.0, MemMachine 91.7) sits at the measurement ceiling and is not mutually rankable. **Roadmap consequence: LoCoMo is demoted to internal regression guard; it is no longer a SOTA target.** Our 84.5% is near the benchmark's effective discriminative limit.
3. **LongMemEval-V2 shipped** (arXiv:2605.12493, same authors): 451 questions, ≤500 sessions / 115M tokens, multimodal, and a **latency-priced headline metric (LAFS Gain** — accuracy-latency frontier over 1-200s budgets). Best released baseline 74.9%; **leaderboards still unpopulated** — a land-grab. Consequence: latency becomes a *scored* axis, which converts §7's tiering table from product hygiene into leaderboard strategy, and prices the cross-encoder stage (§3 measurement).
4. **Answerer-model dependence is now externally quantified**: Hindsight's ACL 2026 demo discloses its own ladder — LME **83.6 with a 20B open model → 91.4 with Gemini-3 Pro → 94.6 top config** (~11 points from the answerer alone). This independently confirms the M45 hypothesis and sets the honest comparison band: our 74.4 with gpt-4o-mini answerer+judge vs their 83.6 with a 20B open answerer.
5. **Retrieval-stage tuning beats architecture** (MemMachine ablation, arXiv:2604.04853): retrieval depth +4.2, context formatting +2.0, search-prompt design +1.8, query-bias correction +1.4. Independently corroborates the M46 emphasis; adds one cheap candidate — **neighbor-episode context expansion** (§3 item 5).
6. **Field convergence, no rearchitecture required**: sleep-time/offline consolidation is the dominant 2026 theme (SCM arXiv:2604.20943; Nemori accepted to ACL 2026; ChatGPT "Dreaming," 2026-06-04) — Astrocyte's observations-with-trends + mental-model compile already are this tier; the field's delta (offline scheduling, value-based forgetting, future-utility scoring) is an incremental M49+ lane. Model-native memory is **no threat in a 12-month window**: DeepSeek shipped V4 *without* Engram; the credible line (Cartridges at Scale arXiv:2606.04557, Still arXiv:2606.07878) compiles per-corpus KV artifacts, not per-user memory. Watch item only.

**Banked evidence constraints (do not retry):**
- More parallel RRF siblings anti-compose past ~4 (M18b, M31c).
- Agentic reflect without termination fix is net-negative (M20 all-on; M36/v015o hybrid: 62.2%, worst of series).
- Prompt bundles are system-level: partial application of M44 addenda erased the win (v015w-fix, −9q).
- top_50 beats top_200 (context dilution); mt_8192 is the ship-floor cutoff.
- Intent-weighted RRF plumbing works; the regex classifier misroutes (v015k) — classifier investment is speculative (Hindsight: none, 94.6%).

## 1. Gap decomposition

| Gap | Evidence | Owner phase |
|---|---|---|
| Answerer strength (never measured as a matrix) | m13.1: gpt-4o 76.7% vs mini ~60% same config; **externally confirmed: Hindsight's disclosed ladder = ~11 LME points from answerer alone (83.6→94.6)** | **M45** |
| Retrieval precision residue | M31c/M33 bank: reranker-large, entity-overlap boost, factoid extraction | **M46** |
| Embedding quality (topic conflation) | JEPA/CCA research thread; unstarted; `local_embeddings` provider makes gating cheap | **M46** |
| Cross-session reasoning (TR/MS categories) | M36 problem statement stands; both failures were termination-architecture, not thesis | **M48a** (gated) |
| Public credibility | Methodology doc rule 1 — now institutionalized by AML's matched harness (self-reports discounted ~30-45 points there) | **M47** (AML cycle 2 + LME-V2) |
| Task-memory execution | MemoryArena: ALL published systems ≤0.25 SR — greenfield | **M48b** (gated) |

## 2. M45 — Phase 0: answerer-strength matrix

**Hypothesis:** a material fraction of the 20pp LME gap is answerer-bound, not architecture-bound.

| # | Decision | Locked value |
|---|---|---|
| 1 | Scope | Answerer model ONLY; retrieval stack frozen at v015w config |
| 2 | Matrix | {gpt-4o-mini (baseline), gpt-4.1-mini, claude-haiku-4.5 (via claude-cli track), gpt-4o} × {LME n=90, LoCoMo n=200} @ mt_8192 |
| 3 | Judge | gpt-4o-mini, unchanged (isolates the answerer variable) |
| 4 | Budget | ~$50 API + subscription quota for the Haiku cell |
| 5 | Comparability | Non-baseline cells are a NEW ablation track; do not extend BENCH_PARITY gpt-4o-mini history |
| 6 | Decision rule | If best cell ≥ +8pp LME over baseline → answerer-bound; Quality tier (§7) adopts it and M48a headroom shrinks accordingly. If < +4pp → architecture-bound; M48a priority rises. |

Also closes out: the orphaned claude-native smoke (fully local pipeline) — rerun at per-type 3 to record pace/quota/score.

**Side measurement (cheap, LME-V2 prep):** record per-question wall latency in every M45 cell so we can plot our first accuracy-latency frontier — LongMemEval-V2's headline metric (LAFS Gain) prices latency, and we have never measured our own curve.

## 3. M46 — Phase 1: retrieval residue + embedding gate

One variable per cycle, flags default OFF, n=90 LME + n=200 LoCoMo, 2-run means, M31-style gates:

1. **bge-reranker-large** swap (M33 direction #1) — precision without pool inflation.
2. **Entity-overlap boost** (M33 #2) — keyword signal without a 5th RRF sibling.
3. **Extraction factoid fix** (M33 #3) — "I have a Y" discrete facts; targets SSU misses that were extraction failures.
4. **Embedding gate**: (a) linear-CCA sanity check on (chunk, fact) pairs with production embeddings — afternoon, CPU-only; (b) if signal, BGE-M3 A/B via provider config (one cycle); (c) MemoryJEPA fine-tune ONLY if (a)+(b) pass.
5. **Neighbor-episode context expansion** (NEW — MemMachine ablation evidence, arXiv:2604.04853): expand nucleus fact hits with adjacent-turn context from the same session before the answerer. Their ablation attributes +4.2 to retrieval-depth-style tuning; our section anchors (`document_id`,`line_num`) make this a cheap SQL join, not new infrastructure.
6. **Rerank on/off latency frontier** (measurement, not a ship item): quantify the cross-encoder stage's accuracy-vs-latency contribution — under LAFS-style scoring (§0b.3) a stage that buys +1q for +2s may be net-negative on LME-V2 while positive on v1.

Ship gate per item: ≥ +1σ over v015w on the target bench, no >1σ regression on the other.

## 4. M47 — Phase 2: matched-harness credibility (REWRITTEN per §0b)

Priority order changed by the survey; the centerpiece is now AML, with a hard date.

### Cycle-1 industry-track results (retrieved 2026-09-01 from the leaderboard API — closes the roadmap's #1 knowledge gap)

**Composite** = unweighted mean accuracy (`average_score` == `accuracy` == `llm_score`), 0-100, over `leaderboard_suite` / `public_suite_v3`, evaluator `official-benchmark-pipelines-v3`. Entries are labelled either `Submitted (API)` (participant-run) or `Evaluated` (platform-run against a public system).

| # | System | Score | A | B | C | D | E | G | H |
|---|---|---|---|---|---|---|---|---|---|
| 1 | MemoraX | **58.02** | 89.9 | 63.4 | 60.0 | 51.2 | 58.5 | 30.0 | 58.4 |
| 2 | MemOS | 45.89 | 68.9 | 53.4 | 56.5 | 44.3 | 48.7 | 9.8 | 56.1 |
| 3 | NTES-MEMORY-SMART | 44.21 | 55.6 | 46.8 | 20.6 | 31.2 | 57.0 | 27.7 | 29.0 |
| 4 | AML-Eval-FLASH | 43.65 | 55.4 | 43.7 | 22.3 | 30.9 | 56.1 | 27.3 | 29.7 |
| 5 | Cognee | 42.61 | 53.6 | 42.9 | 22.0 | 32.1 | 54.5 | 27.1 | 29.0 |
| 6 | NTES-MEMORY-MQ-LAYER | 42.11 | 52.3 | 41.8 | 21.5 | 32.1 | 53.4 | 27.8 | 32.9 |
| 7 | TencentDB | 41.48 | 50.4 | 39.1 | 21.9 | 31.5 | 53.1 | 28.7 | 34.8 |
| 8 | Mem0 | 41.40 | 50.6 | 42.7 | 17.4 | 36.0 | 55.3 | 27.4 | 37.4 |
| 9 | MemPalace | 39.48 | 58.8 | 51.6 | 30.6 | 33.2 | 57.1 | 6.9 | 43.9 |
| 10 | **Vectorize Hindsight Cloud** | **38.54** | 46.4 | 40.4 | 16.9 | 34.8 | 55.7 | 24.3 | 39.4 |
| 11 | memory-dense | 37.65 | 44.9 | 33.0 | 17.8 | 30.0 | 51.7 | 26.7 | 32.3 |
| 13 | memory-8000 | 34.22 | 51.7 | 37.4 | 30.3 | 25.9 | 51.5 | 9.0 | 33.2 |
| 14 | SuperMemory | 29.63 | 28.4 | 26.2 | 14.0 | 24.7 | 51.1 | 25.4 | 47.7 |

**Three findings that set our targets:**

1. **Hindsight Cloud scores 38.54 here** — the same system that self-reports **94.6% LongMemEval**. That single row is the strongest available evidence that unmatched self-reports are not measurements. Mem0 (self-reports 92.5/94.4) lands 41.40.
2. **Realistic bands:** ~38-45 is the crowded middle (where the best-known names sit); **>45.89 beats MemOS**; **>58.02 leads outright**. Our internal 74.4% LME with the weakest common answerer is *not* comparable to these, but it means entering mid-pack is a plausible floor rather than an aspiration.
3. **Capability G is the field-wide failure** (6.9-30.0; MemOS 9.8, MemPalace 6.9, leaf G4 at 0.0-9.0) and **C is second** (13-30 for everyone except MemoraX's 60). MemoraX's entire 12-point lead is concentrated in A (89.9 vs ~50) and C (60 vs ~20). **G and C are therefore the differentiating axes** — an unusually clear target for M46's retrieval work, and a far better-specified goal than "raise LME by 2pp".

**Remaining gap:** the A-H capability legend is SPA-rendered and not exposed via API; resolve it before tuning toward G/C (see §10).

1. **AML cycle-2 submission (opens 2026-09-20 — schedule-driving).** Build the Add/Search adapter (their contract: systems expose only `Add` and `Search`; the platform owns answerer/judge/scoring; Search must return memories, not answers). Astrocyte's `retain()`/`recall()` map directly. Context: 69 frameworks in cycle 1; top score 58.02; best OSS 45.06. **Beating 45.06 makes Astrocyte the top open-source memory framework on the only multi-institution matched harness in existence** — a stronger claim than any self-reported 90s number. Pre-work: pull their full cycle-1 score table and metric composition before tuning anything toward it.
2. **LongMemEval-V2 submission** (~1 wk incl. multimodal triage): 451 questions, LAFS Gain headline metric, **leaderboards currently empty** — early presence is cheap and durable. Our latency data from M45/M46 feeds directly into the LAFS frontier.
3. **AMA-Bench adapter** (~2-3 days): HF leaderboard live since March 2026 (top: GPT-5.2 raw at 0.7226 avg SR); typed A/B/C/D diagnostics remain the value.
4. **Mem0 open harness** (demoted to optional): still useful for one-to-one comparison against their published Table 1, but AML supersedes it as the credibility instrument.
5. **LoCoMo**: retained ONLY as an internal regression guard (n=200, mt_8192). Per the Penfield audit (§0b.2) it cannot rank systems above ~85%; no public claims will be staked on it.

## 5. M48 — Phase 3 (both sub-items gated)

**M48a — reflect v3 (gate: M45 shows ≥8pp remaining headroom).** Termination architecture FIRST: forced candidate answer every iteration, hard 2-pass cap, no `done` tool reliance. Routed to TR/MS only via the shipped `_reflect_routing` signal. Validate as a bundle (M44 lesson).

**M48b — MemoryArena (gate: AMA-Bench ≥50%).** Progressive Search domain first (search API only). Every published system ≤0.25 SR; first credible 0.35+ on any domain is a larger SOTA claim than +2pp LME. Est. 2-3 weeks including agent-execution loop.

## 6. Ecosystem lane (parallel; zero bench risk)

| Item | Complexity | Value |
|---|---|---|
| OKF bundle **export** of wiki pages (frontmatter from type/tags/provenance; `log.md` from revisions; `index.md` from tree) | Low | Interop with Google toolchain; "OKF-conformant" positioning |
| `stale_after` + `status` + human/machine trust tiers on wiki pages | Low | Governance/enterprise |
| `astrocyte-mcp` composed-context tool (single-call wiki page + top facts per entity, cursor pagination — OpenMetadata `get_asset_context` shape) | Med | MCP is the de-facto agent integration path |
| OKF **import** | Med | DEFER: spec broke v0.1→v0.2 in 3 months; no third-party bundles exist yet |

## 7. Cost/latency/accuracy tiering doctrine

Historical pattern: every shipped accuracy win was cost-neutral or cost-negative (routing, segmentation, temporal-at-retain, top_50). The next levers cost money → tier, don't unify:

| Tier | Config | $/q | Latency/q | Accuracy |
|---|---|---|---|---|
| Edge | claude-cli Haiku + local_embeddings, top_50 | ~$0 marginal | +1-3s CLI overhead | TBD (M45 Haiku cell + smoke) |
| Standard | gpt-4o-mini, top_50, mt_8192 | ~$0.004-0.009 | 10-20s (answerer-dominated) | LME 74.4 / LoCoMo 84.5 |
| Quality | M45 winner, same retrieval | ~2-5× Standard | ≈same | M45 measures |
| Max | Quality + reflect-v3 on TR/MS | +~$0.0005/q | +9-10s on ~30% of questions | target LME ≥85% |

Principles: (1) routing/calibration before model spend; (2) never pay for breadth past top_50; (3) subscription CLI = exploration volume, API = ship gates; (4) no matched harness → no SOTA claim; (5) **latency is now externally scored** (LME-V2 LAFS Gain) — every accuracy lever must report its latency cost, and the tier table above doubles as our leaderboard configuration menu (Standard ≈ the LAFS sweet spot; Max only where the budget curve rewards it).

## 8. Projected outcome (revised per §0b)

- **Internal**: LME-v1 ~80-85% after Phases 0-1 at evidence midpoints (LoCoMo held as regression guard near its ~85% discriminative limit).
- **Public — the actual SOTA definition now**: (a) an AML cycle-2 composite above 45.06 = top open-source memory framework on the field's only matched harness; (b) early rows on the empty LME-V2 leaderboards with a competitive LAFS frontier; (c) AMA-Bench presence with per-category diagnostics.
- **Open-field headline**: MemoryArena (M48b) remains untouched by every vendor surveyed — first credible ≥0.35 SR on any domain stands.
- Vendor self-reports in the 90s are no longer the bar to clear: under matched conditions the entire field sits at ≤58 composite, and our honestly-harnessed numbers may already be more competitive than the raw comparison suggested.

## 9. Design consequences (added 2026-09-01)

1. **Promote structured result rendering into core `recall()`** (optional `render: "structured"` — the M28-B `Fact N / When / Confidence / Source chunk` format currently bench-only in `_hindsight_answerer.py`). Under AML the platform's answerer consumes our Search output verbatim: the memory bundle IS the product. Also closes the M32 parity arc — the AML adapter calls public `Astrocyte.recall()` only.
2. **`recall()` synthesis-free invariant.** AML's "Search must not generate answers" rule elevates the recall-vs-reflect split from convention to compliance property. Guarantee: no LLM-generated text in recall hits — only retained/derived memory with provenance. Document as an API invariant.
3. **Latency budget as a runtime parameter** (`recall(latency_budget=…)` / execution profiles) with stage-level degradation (skip cross-encoder + expansions under tight budgets). LAFS scores the frontier; a budget-aware pipeline produces the whole curve, and §7's tiers become execution paths through one pipeline.
4. **Reconstruction-at-recall as a named stage**: nucleus hit → bounded context expansion at query time (neighbor turns / same-session / entity-linked), leveraging section anchors — the general form of M46 #5, aligned with the ground-truth-preservation trend.
5. **Unified consolidation scheduler (M49+ shape decision now)**: observation consolidation + mental-model refresh + wiki compile under one offline scheduler with value-based-forgetting hooks; the OKF-aligned `stale_after`/`status`/trust fields (§6) double as forgetting-policy inputs. Note: `retained_at`/`occurred_at` already constitutes TOKI-style bitemporality (arXiv:2606.06240) — adopt the vocabulary, build nothing.
6. **Known gap**: LME-V2 is multimodal; the `caption_then_embed` path is spec'd but unexercised — first real test is the M47 #2 submission.

## 10. Open questions (blocking-ish, cheap to resolve)

1. **A-H capability legend.** The leaderboard exposes per-capability scores but not their definitions (`/api/capabilities` 404s; the legend is SPA-rendered). G and C are the differentiating axes — we should not tune toward them blind. Resolve by reading the rendered leaderboard UI or the AML paper/docs.
2. **Open-source track.** Only the `industry` track was returned by the API (15 entries). Cycle-1 reporting cited ~69 frameworks and a best-OSS of 45.06 (InvMem), so an OSS track exists somewhere — determine which track Astrocyte should enter, since "top open-source" is the more attainable and more defensible claim.
3. **Add-side cost/latency of a full evaluation run.** ~1,500 histories and ~5,000 questions, batched at ≤20 messages / 2,000 words per Add. Our retain path is LLM-heavy (fact extraction + tree summaries + embeddings). Estimate total ingest spend under each provider config BEFORE requesting evaluation access; this is the real budget question for the submission.
