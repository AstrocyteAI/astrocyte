---
title: "SOTA roadmap — M45-M48 (answerer matrix → retrieval residue → matched harness → reasoning/MemoryArena)"
draft: false
topic: design
---

# SOTA roadmap — M45-M48

**Status:** PROPOSED (2026-09-01; revised same day after the AI-memory landscape survey — see §0b; revised 2026-09-02 with the local self-evaluation harness — see §4b, §9.7)
**Predecessor:** v0.15.1 (cycle `v015w` ship-floor: LME 74.4% @ mt_8192 n=90; LoCoMo 84.5% n=200 / 82.1% n=1540)
**Goal:** credible SOTA positioning across the matched-harness leaderboards (AML, LongMemEval-V2, AMA-Bench, MemoryArena), with an explicit cost/latency/accuracy tiering doctrine.
**Hard date:** AML submission cycle 2 opens **2026-09-20** (§4).
**Implementation status:** the AML adapter + self-eval harness described in §4/§4b live on branch **`feat/aml-adapter`** (worktree `astrocyte-wt-aml`, HEAD `fd979d2`) — **not merged to `main`**, so they are invisible from a default checkout. 68 tests pass, CI-gated. Everything else in this doc is proposal.

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
| 2 | Matrix | {gpt-4o-mini (baseline), gpt-4.1-mini, claude-haiku-4.5 (via claude-cli track), gpt-4o} × {LME n=90, LoCoMo n=200} @ mt_8192 — **see availability table below** |
| 3 | Judge | gpt-4o-mini, unchanged (isolates the answerer variable) — **currently credit-blocked** |
| 4 | Budget | ~$50 API + subscription quota for the Haiku cell |
| 5 | Comparability | Non-baseline cells are a NEW ablation track; do not extend BENCH_PARITY gpt-4o-mini history |
| 6 | Decision rule | If best cell ≥ +8pp LME over baseline → answerer-bound; Quality tier (§7) adopts it and M48a headroom shrinks accordingly. If < +4pp → architecture-bound; M48a priority rises. |

### Reachability as of 2026-09-03 (blocking; revised from the 2026-09-01 plan)

The matrix above was written assuming OpenAI API access. That assumption no longer holds, and
three of four answerer cells **plus the judge** are gated:

| Cell | Status | Route |
|---|---|---|
| gpt-4o-mini (baseline **and judge**) | ❌ **blocked** | API only; key returns HTTP 429 (no credits). Unreachable locally: Codex CLI on a ChatGPT account serves **only `gpt-5.5`** (every other model id rejected, incl. `gpt-4o-mini`, `gpt-5-mini`, `o4-mini`); Ollama has no OpenAI models. |
| gpt-4.1-mini | ❌ blocked | same |
| gpt-4o | ❌ blocked | same |
| claude-haiku-4.5 | ✅ **DONE** | see below |
| gpt-5.5 (NEW cell, not in the original plan) | ✅ available | Codex CLI, ChatGPT subscription auth |

**The Haiku cell is already measured.** It did not stay a smoke: it ran at **n=48
(per-type 8), LME 83.3% @ mt_8192**, zero CLI failures over ~20h, with a full record —
config, all four cutoffs, per-type deltas, operational notes — in
[`claude-native-ablation.md`](claude-native-ablation.md). Do **not** re-run it.

Two findings from that cell feed directly into this roadmap:

1. **Judge sensitivity is partially answered.** All 48 answers were re-graded by **gpt-5.5 via
   Codex** using the harness `JUDGE_PROMPT` verbatim: **91.7% agreement, and all four
   disagreements favoured gpt-5.5** (83.3% → 91.7%). A cross-vendor frontier judge was *more*
   generous than Haiku, so the judge-leniency hypothesis is falsified in the testable direction.
   Tool: `astrocyte-py/scripts/cross_judge_codex.py`, reusable on any results dir.
2. **Temporal-reasoning regressed to 62.5%** — the only category that got worse, and **both
   judges scored it identically (5/8)**, so it is a real weakness rather than judging noise.
   Consistent with date arithmetic depending on temporal-resolution-at-retain. **This is a
   direct M46 input** (§3).

**Revised M45 scope.** The answerer-strength hypothesis cannot be settled without OpenAI
credits, because the baseline cell *is* gpt-4o-mini. What remains executable now:

- **r4 (highest value, ~$12, ~4h):** re-run the *existing* claude-native memories with
  gpt-4o-mini as answerer **and** judge. Ingest already exists in the bench DB, so this is
  answer+judge only. Everything then matches the v015w baseline except the memory pipeline,
  making any delta attributable to memory quality — the single experiment that separates
  memory quality from answerer strength. **Blocked on credits only.**
- **gpt-5.5 answerer cell (free):** available today via Codex; adds an upper anchor to the
  ladder but does not restore the baseline, so it informs the tiering table (§7 Quality row)
  rather than the isolation question.

Until credits exist, **the 83.3% must not be compared to the 74.4% baseline** — four variables
differ (answerer, judge, embedder, concurrency). See `claude-native-ablation.md` §5.

**Side measurement (cheap, LME-V2 prep):** record per-question wall latency in every M45 cell so we can plot our first accuracy-latency frontier — LongMemEval-V2's headline metric (LAFS Gain) prices latency, and we have never measured our own curve.

## 3. M46 — Phase 1: retrieval residue + embedding gate

> **PREREQUISITE — resolve the A-H capability legend before tuning toward G/C (§10.1).**
> §4 identifies G and C as the differentiating axes, but the leaderboard exposes
> per-capability *scores* without their *definitions* (`/api/capabilities` 404s; the legend
> is SPA-rendered). Tuning retrieval toward two unlabelled columns is the one place this
> plan can waste a full cycle: every item below would be selected and gated against a
> target we cannot read. Cost to resolve is near zero (read the rendered UI, the AML paper,
> or `/api/openapi.json`). **Items 1-3 and 5 are safe to run regardless** — they target
> known internal failures (M31c/M33 bank, the M45 temporal regression). **Do not select or
> prioritise work *because* it should raise G or C until the legend is known.**


One variable per cycle, flags default OFF, n=90 LME + n=200 LoCoMo, 2-run means, M31-style gates:

1. **bge-reranker-large** swap (M33 direction #1) — precision without pool inflation.
2. **Entity-overlap boost** (M33 #2) — keyword signal without a 5th RRF sibling.
3. **Extraction factoid fix** (M33 #3) — "I have a Y" discrete facts; targets SSU misses that were extraction failures.
4. **Embedding gate**: (a) linear-CCA sanity check on (chunk, fact) pairs with production embeddings — afternoon, CPU-only; (b) if signal, BGE-M3 A/B via provider config (one cycle); (c) MemoryJEPA fine-tune ONLY if (a)+(b) pass.
5. **Temporal-resolution repair** (NEW — from the M45 Haiku cell): temporal-reasoning fell to
   **62.5%**, confirmed by both judges (§2). The stack resolves relative dates at retain time;
   the cell suggests that path is weaker under a non-OpenAI extractor. Audit
   `temporal_resolution` / `temporal_dateparser` coverage on claude-native-extracted facts
   before assuming a retrieval fix. Cheap, and it targets a measured regression rather than
   a hypothesised one.
6. **Neighbor-episode context expansion** (NEW — MemMachine ablation evidence, arXiv:2604.04853): expand nucleus fact hits with adjacent-turn context from the same session before the answerer. Their ablation attributes +4.2 to retrieval-depth-style tuning; our section anchors (`document_id`,`line_num`) make this a cheap SQL join, not new infrastructure.
7. **Rerank on/off latency frontier** (measurement, not a ship item): quantify the cross-encoder stage's accuracy-vs-latency contribution — under LAFS-style scoring (§0b.3) a stage that buys +1q for +2s may be net-negative on LME-V2 while positive on v1.

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

### The `academic` track — where Astrocyte should enter (retrieved 2026-09-01)

The open-source track is named **`academic`**, not "open source". Correct query:
`GET /leaderboard?track=academic&benchmark_type=textual`. Valid enums (from the API's own 422 validation): `track ∈ {industry, academic}`, `benchmark_type ∈ {textual, coding}`. Full spec at `/api/openapi.json`.

**50 entries. Crucially, most submit code, not a hosted service**: 39 `Submitted (repo)`, 6 `Submitted (API)`, 5 platform-`Evaluated`. Repo submission likely removes the "deploy and fund a public HTTPS endpoint" burden — confirm mechanics, but our adapter serves either path.

| # | System | Score | Repo |
|---|---|---|---|
| 1 | InvMem | **45.06** | `wenxiaof345-ctrl/vanilla-rag-memory` |
| 2 | Refind | 44.97 | `imlrz/ReFind` |
| 3 | ActiveMemoryIndex | 44.84 | `linxuhao/ActiveMemoryIndex` |
| 4 | Hybrid Search v2.0 | 44.57 | `cydd-1972/hybrid_search` |
| 5 | ChronoHybridMem | 44.33 | `Tin11Mn/chrono-hybrid-mem` |
| 6 | Hybrid Episodic Memory | 44.28 | `tlysanhuo/agent-memory-challenge` |
| 7 | Chronicle | 44.19 | `simple-boy/Chronicle-Memory` |
| 8 | FlowGrid_AML_Retriever | 43.98 | `dlxeva/flowgrid-aml-retriever` |
| 9 | LLLMemoryAgent | 43.06 | `llLAlisa/memory-agent-submission` |
| 10 | aml-memory-mvp | 42.95 | `0xboyu/aml-memory-mvp` |

Distribution: top **45.06** · rank-10 42.95 · median 40.78 · min 23.55.

**Why this reframes the goal.** The #1 open-source system's repo is named `vanilla-rag-memory`, and the entire top 10 spans **2.1 points**. These are hackathon-grade entries; Astrocyte brings a three-engine pipeline, RRF fusion, cross-encoder rerank, typed section links, and observation/wiki tiers. **Target: >45.06 = #1 open source.** Mid-pack (~41) is the floor, not the ambition.

**Academic-track capability ranges (min / median / max)** — the concrete M46 targets:

| | A | B | C | D | E | G | H |
|---|---|---|---|---|---|---|---|
| min | 17.3 | 14.6 | 8.6 | 14.4 | 40.3 | 3.5 | 22.6 |
| median | 51.0 | 41.0 | **19.5** | 32.1 | 53.5 | **27.5** | 32.9 |
| max | 63.5 | 53.1 | **37.9** | 38.7 | 58.3 | **30.9** | 67.7 |

**C and G are the field's walls** — C caps at 37.9 here (vs MemoraX's 60 in industry) and G at 30.9; nobody in open source has cracked either. **H has a lone 67.7 outlier against a 32.9 median**, so one entrant found something specific there worth understanding. These three columns are where a differentiated architecture separates.

**The `coding` track is empty** (`benchmark_type=coding` → 0 entries) despite 12 repos / 1,290 tasks being built. A second land-grab to watch.

### Merged field — we measure against BOTH tracks

Cross-track comparison is legitimate: every entry in both tracks reports
`dataset_suite_version: public_suite_v3`, `evaluator_id: official-benchmark-pipelines-v3`,
`score_scale: 0-100`. The tracks differ in *who submits* (commercial API vs
open repo), **not** in what is measured. **65 systems total** (15 industry + 50 academic).

Merged top 20:

| # | Track | System | Score |
|---|---|---|---|
| 1 | IND | MemoraX | 58.02 |
| 2 | IND | MemOS | 45.89 |
| 3 | ACA | InvMem | 45.06 |
| 4 | ACA | Refind | 44.97 |
| 5 | ACA | ActiveMemoryIndex | 44.84 |
| 6 | ACA | Hybrid Search v2.0 | 44.57 |
| 7 | ACA | ChronoHybridMem | 44.33 |
| 8 | ACA | Hybrid Episodic Memory | 44.28 |
| 9 | IND | NTES-MEMORY-SMART | 44.21 |
| 10 | ACA | Chronicle | 44.19 |
| 11 | ACA | FlowGrid_AML_Retriever | 43.98 |
| 12 | IND | AML-Eval-FLASH | 43.65 |
| 13 | ACA | LLLMemoryAgent | 43.06 |
| 14 | ACA | aml-memory-mvp | 42.95 |
| 15 | IND | Cognee | 42.61 |
| 16 | ACA | TraceMem | 42.53 |
| 17 | ACA | Memoria | 42.33 |
| 18 | ACA | Raw Memory | 42.31 |
| 19 | ACA | fenghe | 42.26 |
| 20 | ACA | AMC-Memory | 42.22 |

Mem0 (41.40) and Vectorize Hindsight Cloud (38.54) fall **outside the merged top 20** — behind a dozen anonymous academic entries.

**Score → placement (the target table for M46/M47):**

| Score | Merged rank | Meaning |
|---|---|---|
| 41 | ~33rd | mid-pack; ahead of Hindsight Cloud, roughly level with Mem0 |
| 43 | ~14th | top quartile |
| 45.1 | **3rd** | **#1 open source** (clears InvMem 45.06) |
| 46 | **2nd** | clears MemOS — best system on the board except MemoraX |
| 50 | 2nd | still 2nd; MemoraX's 58.02 is a wide moat |

**Positioning consequence.** Two claims are available at different price points:
"#1 open-source memory framework" needs **>45.06**; "#2 overall, ahead of every
commercial system except MemoraX" needs **>45.89** — only 0.83 points more.
Both are far cheaper than the 58.02 outright lead, and both are more defensible
than any unmatched self-report. Report our result against the merged field, not
only the academic track.

1. **AML cycle-2 submission (opens 2026-09-20 — schedule-driving).** Build the Add/Search adapter (their contract: systems expose only `Add` and `Search`; the platform owns answerer/judge/scoring; Search must return memories, not answers). Astrocyte's `retain()`/`recall()` map directly — implemented in `astrocyte-services-py/astrocyte-aml-py` on branch `feat/aml-adapter` (68 tests passing, CI-gated; **unmerged**), together with the local self-evaluation harness described in §4b. **Enter the `academic` track; beat 45.06 for #1 open source** — a stronger claim than any self-reported 90s number.
2. **LongMemEval-V2 submission** (~1 wk incl. multimodal triage): 451 questions, LAFS Gain headline metric, **leaderboards currently empty** — early presence is cheap and durable. Our latency data from M45/M46 feeds directly into the LAFS frontier.
3. **AMA-Bench adapter** (~2-3 days): HF leaderboard live since March 2026 (top: GPT-5.2 raw at 0.7226 avg SR); typed A/B/C/D diagnostics remain the value.
4. **Mem0 open harness** (demoted to optional): still useful for one-to-one comparison against their published Table 1, but AML supersedes it as the credibility instrument.
5. **LoCoMo**: retained ONLY as an internal regression guard (n=200, mt_8192). Per the Penfield audit (§0b.2) it cannot rank systems above ~85%; no public claims will be staked on it.

## 4b. Local self-evaluation before submitting (added 2026-09-02)

We can produce a directional AML-methodology number **before** requesting
evaluation access, which de-risks the cycle-2 window. Shipped in
`astrocyte-services-py/astrocyte-aml-py/aml_selfeval/`.

**What AML publishes, and what it doesn't.** Their `data/<bench>/pipeline.py`
files ship the **answer** and **judge** halves only: each consumes an `--input`
JSONL whose records *already contain retrieved memories*. Retrieval runs on
their orchestrator against each participant's Add/Search — so the retrieval half
is the piece we had to build:

```
ingest (Add) → retrieve (Search) → AML-shaped input JSONL
             → AML's answer prompt → AML's judge rubric → score
```

**Why it is worth more than our internal bench.** AML's answer prompt and judge
rubric are used *verbatim*, and retrieval goes through the same `/add` +
`/search` contract the platform will exercise. **What it cannot replicate:**
AML's private answerer/judge model choice, and four of the six suite benchmarks
(`personamem`, `clbench`, `scriptmem`, `beam`) whose data is unpublished. Treat
the output as **directional, not a predicted placement** — `beam` in particular
(10M-token degradation testing) is a capability we have never exercised.

**Two findings from building it:**

1. **AML's published pipelines cannot run as shipped.** All six contain
   `async with httpx.AsyncClient(...) as client, output.open(...) as handle:` —
   `Path.open()` is a *synchronous* context manager, which `async with` rejects
   on every Python 3.x (9 sites). The defect is confined to the driver loop; the
   prompt and judging logic are sound. `aml_selfeval.judge` therefore imports
   `render_answer_prompt` / `render_accuracy_prompt` / `parse_judge_label`
   verbatim and supplies its own loop, issuing byte-identical
   `/chat/completions` requests. Worth raising upstream — it blocks anyone
   trying to reproduce AML results locally.
2. **Their harness assumes an OpenAI-compatible endpoint, not OpenAI.** The
   dependency is a wire format (one POST, three fields), so
   `aml_selfeval.shim` serves that endpoint backed by any provider the SPI
   resolves (`astrocyte.llm_providers`). See §9.7.

**Measured ingest cost:** LongMemEval at n=90 = **6,056 Add calls**, each running
the full retain pipeline (fact extraction + tree summary + embeddings). Partially
answers §10.3.

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

Principles: (1) routing/calibration before model spend; (2) never pay for breadth past top_50; (3) subscription CLI = exploration volume, API = ship gates; (4) no matched harness → no SOTA claim; (5) **latency is now externally scored** (LME-V2 LAFS Gain) — every accuracy lever must report its latency cost, and the tier table above doubles as our leaderboard configuration menu (Standard ≈ the LAFS sweet spot; Max only where the budget curve rewards it); (6) **the tier is a provider choice, never a harness constraint** — any external harness's model assumption is shimmed at the wire boundary (§9.7), so every row above is runnable on every bench, and the Edge row is now measurable under AML methodology via §4b rather than staying TBD.

## 8. Projected outcome (revised per §0b)

- **Internal**: LME-v1 ~80-85% after Phases 0-1 at evidence midpoints (LoCoMo held as regression guard near its ~85% discriminative limit).
- **Public — the actual SOTA definition now**, measured against the merged 65-system field (both AML tracks, same suite/evaluator): (a) **>45.06 = #1 open source (3rd overall)**; (b) **>45.89 = 2nd overall**, ahead of every commercial system except MemoraX — 0.83 points beyond the first milestone; (c) early rows on the empty LME-V2 leaderboards with a competitive LAFS frontier; (d) AMA-Bench presence with per-category diagnostics. Mid-pack (~41) already places ahead of Hindsight Cloud (38.54) and level with Mem0 (41.40).
- **Open-field headline**: MemoryArena (M48b) remains untouched by every vendor surveyed — first credible ≥0.35 SR on any domain stands.
- Vendor self-reports in the 90s are no longer the bar to clear: under matched conditions the entire field sits at ≤58 composite, and our honestly-harnessed numbers may already be more competitive than the raw comparison suggested.

## 9. Design consequences (added 2026-09-01)

1. **Promote structured result rendering into core `recall()`** (optional `render: "structured"` — the M28-B `Fact N / When / Confidence / Source chunk` format currently bench-only in `_hindsight_answerer.py`). Under AML the platform's answerer consumes our Search output verbatim: the memory bundle IS the product. Also closes the M32 parity arc — the AML adapter calls public `Astrocyte.recall()` only.
2. **`recall()` synthesis-free invariant.** AML's "Search must not generate answers" rule elevates the recall-vs-reflect split from convention to compliance property. Guarantee: no LLM-generated text in recall hits — only retained/derived memory with provenance. Document as an API invariant.
3. **Latency budget as a runtime parameter** (`recall(latency_budget=…)` / execution profiles) with stage-level degradation (skip cross-encoder + expansions under tight budgets). LAFS scores the frontier; a budget-aware pipeline produces the whole curve, and §7's tiers become execution paths through one pipeline.
4. **Reconstruction-at-recall as a named stage**: nucleus hit → bounded context expansion at query time (neighbor turns / same-session / entity-linked), leveraging section anchors — the general form of M46 #5, aligned with the ground-truth-preservation trend.
5. **Unified consolidation scheduler (M49+ shape decision now)**: observation consolidation + mental-model refresh + wiki compile under one offline scheduler with value-based-forgetting hooks; the OKF-aligned `stale_after`/`status`/trust fields (§6) double as forgetting-policy inputs. Note: `retained_at`/`occurred_at` already constitutes TOKI-style bitemporality (arXiv:2606.06240) — adopt the vocabulary, build nothing.
6. **Known gap**: LME-V2 is multimodal; the `caption_then_embed` path is spec'd but unexercised — first real test is the M47 #2 submission.
7. **LLM-agnosticism extends to the evaluation path, not just the product** (added 2026-09-02). Third-party harnesses encode an OpenAI-shaped `/chat/completions` call as though it were a vendor dependency; it is a wire format. The rule: an external harness never dictates our provider — put a shim at the wire boundary and resolve the provider through `astrocyte.llm_providers`, the same names valid in `astrocyte.yaml`. Consequence: no benchmark, ours or anyone's, may be blocked on a specific vendor's credits, and every published number carries an explicit statement of which provider produced it. Fidelity caveat to state whenever we report shim-driven results: providers that cannot honour `temperature=0` (the Claude CLI has no temperature flag) give stable rather than bit-for-bit deterministic runs.

## 10. Open questions (blocking-ish, cheap to resolve)

1. **A-H capability legend — GATES M46 (§3).** The leaderboard exposes per-capability scores but not their definitions (`/api/capabilities` 404s; the legend is SPA-rendered). G and C are the differentiating axes — we should not tune toward them blind. Resolve by reading the rendered leaderboard UI or the AML paper/docs.
2. ~~**Open-source track.**~~ **RESOLVED 2026-09-01**: it is the **`academic`** track (`?track=academic&benchmark_type=textual`), 50 entries, top 45.06. See §4. Follow-up: confirm the `Submitted (repo)` mechanics (39 of 50 chose it) — if code submission is accepted, the hosting/funding burden largely disappears and the deployment decision shrinks to a bench-config choice.
3. **Add-side cost/latency of a full evaluation run.** ~1,500 histories and ~5,000 questions, batched at ≤20 messages / 2,000 words per Add. Our retain path is LLM-heavy (fact extraction + tree summaries + embeddings). Estimate total ingest spend under each provider config BEFORE requesting evaluation access; this is the real budget question. **Partially measured (2026-09-02):** LongMemEval at n=90 = **6,056 Add calls** (§4b) — comparable to our internal n=90 LME bench (~50 min, ~$12) plus answer/judge calls. Extrapolate to the full suite before committing. **Correction to the earlier note here:** this is *not* blocked on OpenAI credits. Per §9.7 the whole self-eval path — ingest, answer, and judge — runs through SPI-resolved providers, so the claude-native stack (`claude_cli` + `local_embeddings`) covers it end to end. What remains genuinely untested is that stack *at evaluation scale*; and the CLI's subscription auth is still awkward for a hosted service, which remains a point in favour of repo submission (§10.2).
4. **The lone H=67.7 outlier** in the academic track (median 32.9). Whatever that entrant did in capability H is the single largest unexplained delta on the board; identify it once the A-H legend is known.
