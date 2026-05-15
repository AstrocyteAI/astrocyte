# M13 close-out and M14 retain pipeline overhaul — roadmap

**Status:** Living plan. M13.0–M13.2 landed; M13.3 close-out in flight at the date of this doc. M14 unstarted.

**Companion docs:**
- [benchmark-comparison-methodology.md](benchmark-comparison-methodology.md) — harness/judge variance principles
- [llm-wiki-compile.md](llm-wiki-compile.md) — Karpathy compile spec; M14 implements the parts we punted on
- [recall.md](recall.md) — current section + fact + wiki recall stack

---

## 1. Where we are

> **Updated 2026-05-14/15.** §1 numbers below are the M13.3 baseline as
> originally written. M14 ran through phases 0–4 and was closed
> 2026-05-15 with a null bench verdict at the locked 2σ gate. **For
> live numbers see §8.9; for the cycle close see §11; for remaining
> Hindsight design gaps see §12.** The "in striking distance" framing
> below has not held up — see §8.2.

**Native bench (M12.6, gpt-4o-mini synth + judge):**
- LongMemEval-S 56.5%, LoCoMo 62.5%

**Mem0-harness M13.3 fair-sample (multi-grain, gpt-4o-mini judge+answerer, Mem0 default prompts, `--user-profile`):**

| Bench | N | top_10 | top_20 | top_50 | top_200 |
|---|---|---|---|---|---|
| LoCoMo (`--max-questions 20` × 10 convs) | 200 | **80.0%** | 77.5% | 80.5% | 82.5% |
| LME (`--per-type 5`) | 30 | — | **60.0%** | — | — |

LME per-category (N=5/cat): knowledge-update 100%, multi-session/single-session-assistant/single-session-user/temporal-reasoning all 60%, single-session-preference 20%.

**Gap to Hindsight (gpt-4o-mini AMB, 92-94%):**
- LoCoMo: ~12-15pp — **in striking distance**, plausibly all real-quality gap
- LME: ~34pp — **materially behind**, driven by gpt-4o-mini answerer's worse synthesis over candidates (LME's preference/multi-session questions need more reasoning over the candidate list than LoCoMo's direct-lookup questions). M13.1 gpt-4o LME N=30 scored 76.7% top_20; the 16.7pp drop is purely the answerer+judge tier swap.

**Reference points (see [benchmark-comparison-methodology.md](benchmark-comparison-methodology.md) for variance caveats):**
- Mem0 Table 1 cluster on Mem0-harness gpt-4o: LangMem 62, Zep 62, OpenAI 64, Mem0 67
- Hindsight on AMB gpt-4o-mini: LME 94.6%, LoCoMo10 92.0%

**Interpretation:** LoCoMo M13.3 is competitive (~12pp behind Hindsight); LME M13.3 is the gap that needs M14. The asymmetry is informative — our retrieval feeds candidates well for direct-lookup questions (LoCoMo shape), but synthesis-heavy questions (LME shape) bottleneck on the gpt-4o-mini answerer's reasoning over a flat candidate list. **M14.1 multi-output extraction** (assistant_statements grain + supersedes targets in atomic form) directly attacks this by giving the answerer pre-synthesised facts so its synthesis step is shorter.

---

## 2. Critical config choice — gpt-4o-mini end-to-end

Mem0 harness and Hindsight harness diverge on judge + answerer model. Per `hindsight-dev/benchmarks/common/benchmark_runner.py:228`, Hindsight defaults to **gpt-4o-mini for both judge and answerer**. Mem0's `memory-benchmarks/benchmarks/locomo/run.py` defaults to **gpt-4o for both**.

We standardise on **gpt-4o-mini** for ongoing iteration:
- ~16× cheaper per question than gpt-4o (LoCoMo full ~$8 instead of ~$125)
- Same model tier as Hindsight's published 92-94% runs
- Matches our native bench config (no model-tier divergence within the project)

The trade: not directly comparable to Mem0's paper Table 1 cluster (gpt-4o). We accept this — Hindsight is the harder target anyway. **Config B (gpt-4o, strict) is deferred to the end of M14** — single expensive run for the Mem0-paper comparison after retain work has improved our numbers.

Makefile defaults (live):
```
MEM0_HARNESS_ANSWERER_MODEL ?= gpt-4o-mini
MEM0_HARNESS_JUDGE_MODEL    ?= gpt-4o-mini
```

Override via `make bench-mem0-harness-parallel MEM0_HARNESS_ANSWERER_MODEL=gpt-4o ...` when the deferred apples-to-apples-with-Mem0 number is wanted.

### Note on judge lenience (Mem0 harness vs AMB)

A first-pass reading of AMB's per-category judge prompts (off-by-one date tolerance on temporal-reasoning, mixed-info acceptance on knowledge-update) suggested AMB was meaningfully more lenient than Mem0's harness. **That reading was wrong on re-investigation.** Mem0's actual judge prompts (`memory-benchmarks/benchmarks/locomo/prompts.py:218` and `longmemeval/prompts.py:265`) are at least as lenient as AMB's:

| Lenience rule | AMB | Mem0 harness |
|---|---|---|
| Date off-by-one | only temporal-reasoning | **universal — 14-day tolerance on LoCoMo; off-by-one on LME** |
| Partial credit for list answers | no explicit rule | **1-of-N is fine** |
| Anti-no bias | no | **explicit "lean toward yes when in doubt"** |
| Paraphrase = correct | implicit | **explicit with examples** |
| Duration tolerance | only off-by-one | **within 50% on LoCoMo** |

Mem0's own published 91.6% LoCoMo and Hindsight's 92% LoCoMo10 land at similar absolute numbers, which suggests the two harnesses are **roughly equivalent in difficulty**. The remaining gap between our number and Hindsight's is therefore **mostly real quality gap**, not harness lenience — porting AMB-style prompts into our runner would not materially close it (initially I'd suggested it could close 5-10pp; that estimate was wrong, the realistic close is 0-3pp).

M14 retain work is the only path that closes the residual quality gap.

---

## 3. M13.3 — close M13 with a Hindsight-comparable number

Goal: lock in M13.x numbers on the Mem0 harness, gpt-4o-mini config, archive to R2, commit. **No AMB-prompt porting** — Mem0's default judge prompts are already lenient (see §2 note), so swapping doesn't materially help.

### Task list

| Task | Cost | Wall | Gate |
|---|---|---|---|
| Dispatch `make bench-mem0-harness-parallel MEM0_HARNESS_PROJECT=astrocyte-m13.2-mini` | ~$15 | ~2hr | LoCoMo full + LME `--per-type 5` (N=30) |
| Inspect LME N=30 vs M13.1 N=30 baseline | $0 | 5 min | did multi-grain move LME at all? |
| Decide on LME `--all-questions` (full 500q) | ~$8 if yes | ~4hr | gate: N=30 ≥ 65% top_10 |
| Commit M13.x + archive to R2 (`make bench-archive STAGE=m13-close`) | ~$0 | ~10 min | both bench legs landed |
| **Config B (deferred)**: dispatch `gpt-4o` strict run at the END of M14 | ~$165 | ~2hr | run once at end of M14 to lock in Mem0-paper-comparable number |

### Decision gate after LoCoMo full + LME N=30 land

| LoCoMo top_10 | LME N=30 top_10 | Next move |
|---|---|---|
| ≥ 70% | ≥ 70% | Dispatch full LME `--all-questions`; publish gpt-4o-mini number; pivot to M14 |
| 55-70% | 50-70% | Same as above with sharper per-category postmortem |
| < 50% | < 50% | Skip publication; M14 retain work first (extraction is the bottleneck) |
| Mixed | | Judgment call based on which categories are weak |

### Honest framing of expected outcome

Hindsight published 92-94% on AMB. We're hitting the same model tier (gpt-4o-mini) on a roughly-equivalent-difficulty harness (Mem0 with its lenient default prompts). If M13.2 multi-grain lands at 70-80% on Mem0 harness:

- That's the **Hindsight comparison number**, with caveats: different harnesses, but equivalently-lenient judges; Hindsight's lead reflects mostly real quality (their gpt-oss-120b retain LLM + more sophisticated retain pipeline).
- ~12-20pp behind Hindsight on real quality, **not "in striking distance"** — M14 retain is the path that meaningfully closes this.
- Above the Mem0 paper Table 1 cluster (62-68% on gpt-4o judge — but that's a *stricter* judge model context, not directly comparable; the deferred Config B at gpt-4o will pin this number cleanly).

---

## 4. M14 — retain pipeline FSM overhaul

Pure code work, minimal API spend except for re-bench checkpoints.

### Architecture: an FSM scaffold for the retain pipeline

```
INIT
  ↓
READ                          parse source / tokenize
  ↓
┌── EXTRACT_FACTS (multi-pass) ──┐
│   ↓                            │
│   DEDUPE_FACTS                 │   parallel branches —
├── EXTRACT_ENTITIES ─────────────┤   wait at JOIN before
│   ↓                            │   transitioning to next
│   NORMALIZE_ENTITIES           │   stage
└── EMBED ───────────────────────┘
  ↓
IDENTIFY_AFFECTED_WIKIS
  ↓
  ├──[ no affected ]──→ WRITE_NEW_WIKIS (DBSCAN compile — current behaviour)
  └──[ affected ]─────→ UPDATE_AFFECTED_WIKIS (LLM revise each)
  ↓
BUILD_SUPERSEDES_GRAPH        guard: any new fact contradicts old?
  ↓
UPDATE_INDEX_MD               guard: any wiki created or renamed?
  ↓
APPEND_LOG_MD                 always — Karpathy audit trail
  ↓
COMPLETE

Any state → FAILED on hard error (with checkpoint for resume)
```

Built directly in Python under `astrocyte/pipeline/retain_fsm/`. No statewright dependency — the FSM pattern (states / transitions / guards / context / checkpoint) is the abstraction; we own the engine in our repo, in our runtime.

### Phase list

| Milestone | Files touched | Effort | Bench leverage | Ingest cost spike |
|---|---|---|---|---|
| **M14.0 — FSM engine** | new `astrocyte/pipeline/retain_fsm/` (engine, states, context, checkpoint) | 3-5d | enables M14.1-5 | $0 |
| **M14.1 — Single-pass multi-output extraction** | rewrite `section_fact_extraction.py` to emit experiences / preferences / world / plan / opinion / **assistant_statements** + supersedes targets in one structured output | 2-3d | **+10-20pp single-session-assistant, +5-10pp knowledge-update** | $0 — same call count |
| → **Re-bench checkpoint** (Mem0 harness LME `--per-type 5`) | — | $5 | validate direction | — |
| **M14.2 — Karpathy update-affected-wikis** | new `astrocyte/pipeline/wiki_incremental.py`; identifies wikis affected by new source via entity overlap, sends (existing wiki, new content) to LLM with update prompt | 4-5d | **+5-10pp knowledge-update, multi-session** | ~$0.05/source (~$1/bench) |
| **M14.3 — Fact-level supersedes graph** | new migration `021_fact_links.sql`; consume supersedes targets from M14.1; filter superseded facts at retrieval unless query asks about past state | 2-3d | **+3-5pp knowledge-update** | $0 — data from M14.1 |
| **M14.4 — Entity normalisation** | per-doc disambiguation pass in retain FSM; "Dr. Patel" / "Dr Patel" / "Dr. P" → canonical | 2-3d | **+3-5pp multi-session counting, multi-hop** | ~$0.01/doc |
| **M14.5 — index.md + log.md per bank** | new bank-level markdown artefacts; auto-updated by FSM's `UPDATE_INDEX_MD` and `APPEND_LOG_MD` states | 2d | low bench; high product/observability | $0 |
| → **Final M14 re-bench** (full Mem0 harness parallel) | — | $15-30 | locks in M14 number | — |

### M14 total

- Active work: ~3-4 weeks
- Net ingest-cost spike: ~$1-3 per full bench at gpt-4o-mini
- Cumulative bench projection (additive over M13.2 baseline): **+25-45pp on weakest categories** (single-session-assistant, knowledge-update, multi-session); modest impact on already-strong categories

---

## 5. Cost-control principles for M14

Two principles from the cost-investigation in this cycle:

1. **Don't 4× extraction calls naively.** Schema-based single multi-output pass (M14.1) is the right shape — captures 70-80% of multi-pass value at 1× call count.
2. **Conditional gating where appropriate.** M14.2's "identify affected wikis" should not run when no existing wikis cover the source's entities. Gate transitions with cheap checks before expensive LLM calls.

If cost ever becomes the binding constraint mid-M14, fallback options (in order of leverage):
- Drop to a single per-question cutoff in the Mem0 harness (`--top-k-cutoffs 10` instead of 4)
- Pre-compress section text before extraction (RTK-style ANSI/comment strip; cheap, ~10-20% input token reduction)
- Tier extraction passes — cheap classifier first, expensive extraction only on flagged sections

---

## 6. Open questions

1. **Does fact-level supersedes (M14.3) play well with the Mem0 harness's flat candidate list?** The harness has no "this is current state" annotation; superseded facts might still surface and confuse the answerer. May need to filter superseded facts from `search()` output entirely.

2. **Should M14.2 update-affected-wikis run synchronously at ingest, or async via the task queue?** Sync is simpler; async lets the bench run with stale wikis but eventually-correct. Bench impact unclear without measurement.

3. **How aggressively to normalise entities in M14.4?** Too conservative misses count questions; too aggressive merges distinct people. Need a calibration set.

4. **`gpt-oss-120b` via Groq for memory operations (Hindsight's choice)?** Their config uses an open-weights model for extraction. Worth experimenting after M14.1 — could be a future M14.x cost-reduction lever.

---

## 7. Next concrete action

1. Dispatch `make bench-mem0-harness-parallel MEM0_HARNESS_PROJECT=astrocyte-m13.2-mini`.
2. Watch LME N=30 vs LoCoMo full results.
3. Decide on LME `--all-questions` follow-up per gate in §3.
4. Commit M13.x and archive to R2.
5. Begin M14.0 + M14.1 — FSM engine + single-pass extraction.

---

*This document is updated as milestones complete. When M13.3 closes, update §1 with the final M13.x numbers and check off the §7 actions. When each M14.x lands, update the corresponding row in §4 with actual effort vs estimate and actual bench lift vs projection. **2026-05-14/15 retrospective added in §§8–12 supersedes single-run-derived claims in earlier sections.***

---

## 8. M14.0–M14.7 retrospective (2026-05-14/15)

This section captures what actually happened across the M14 cycle, the methodological corrections paid for along the way, the bench-verified phase outcomes, and the final null-verdict close.

### 8.1 Current baseline (4-run clean-DB mean)

**Discovered methodologically (2026-05-14):** prior bench numbers in this doc and earlier commits were contaminated by **accumulated Postgres state across re-runs** (rows from prior bench-banks persisted in the schema; query planner degraded; per-question latency climbed from ~10s to ~315s over ~10 runs without us noticing). Fix in §9. After three baseline runs on freshly-reset DBs plus a fourth verify-revert run:

| | Run 1 | Run 2 | Run 3 | Run 4 (verify) | Mean | Std |
|---|---|---|---|---|---|---|
| **LoCoMo top_10** | 76.0% | 76.0% | 75.5% | 73.5% | **75.25%** | ±1.0pp |
| **LoCoMo top_20** | 80.0% | 79.0% | 76.5% | 77.5% | **78.25%** | ±1.5pp |
| LoCoMo top_50 | 77.5% | 81.0% | 80.0% | 78.5% | 79.25% | ±1.5pp |
| LoCoMo top_200 | 79.5% | 78.5% | 79.0% | 78.5% | 78.88% | ±0.4pp |
| LME top_10 | 70.0% | 73.3% | 70.0% | 70.0% | 71.0% | ±1.6pp |
| **LME top_20** | 63.3% | 63.3% | 63.3% | 70.0% | **65.0%** | ±3.3pp |
| LME top_50 | 70.0% | 60.0% | 70.0% | 73.3% | 68.3% | ±5.6pp |
| LME top_200 | 66.7% | 63.3% | 56.7% | 73.3% | 65.0% | ±7.0pp |

**LME top_20 noise band: ±3.3pp** (originally read as zero across 3 runs; widened after the 4th run produced 70%). LoCoMo top_20: ±1.5pp.

LME 4-run per-category baseline (top_20):

| Category | Mean |
|---|---|
| knowledge-update | ~4.75/5 |
| multi-session | 3.00/5 (flat) |
| single-session-assistant | 3.00/5 (flat) |
| **single-session-preference** | **~1.25/5 (weakest)** |
| single-session-user | 3.75/5 |
| temporal-reasoning | 3.75/5 |

Weakest baselines: **single-session-preference** (~1.25/5) then **multi-session** + **single-session-assistant** (3.00/5 flat).

### 8.2 Drift from §1's M13.3 framing

| | §1 (M13.3) | §8.1 (4-run mean) | Delta |
|---|---|---|---|
| LoCoMo top_10 | 80.0% | 75.25% | **−4.75pp** |
| LoCoMo top_20 | 77.5% | 78.25% | +0.75pp |
| LoCoMo top_50 | 80.5% | 79.25% | −1.25pp |
| LoCoMo top_200 | 82.5% | 78.88% | **−3.6pp** |
| LME top_20 | 60.0% | 65.0% | **+5.0pp** (likely M14.1 assistant_statement contribution) |

LoCoMo drifted **down** ~4pp on top_10 and top_200 — likely intersection of code changes between M13.3 and today. LME drifted **up** ~5pp on top_20. §1's "LoCoMo in striking distance" framing is now wider than written.

### 8.3 M14.2 retrospective — wrong architectural layer

M14.2 built `wiki_incremental.update_affected_wikis_for_document` — entity-overlap join to find wikis whose provenance shares entities with new content, then per-wiki LLM update prompt. Bench evidence across 4 runs (2 with M14.2, 1 with M14.2-revert, 3 clean baselines) was inconclusive due to **DB-state contamination** (since fixed); evaluated on clean DBs, M14.2 was bench-neutral but **architecturally wrong** per the Hindsight comparison (§10.2): prose-wiki updates drift; atomic-fact observation updates are the right shape.

M14.2 module + tests + SPI retained as building blocks; bench wiring reverted.

### 8.4 M14.6 / M14.7 — preference auto-injection retrospective

M14.6 added per-document `preference_compile.py` + `MentalModel(kind="preference")` rows + Mem0-harness profile surfacing. A subsequent **B-1 / B-1.1 / B-1.2** iteration added anchor pinning of preferences at the TOP of the search() result list. Across these variants, **single-session-user regressed 5/5 → 4/5 → 3/5 → 1/5** as the User Profile block bloated with 12+ preference entries per user.

**Hindsight comparison confirmed self-imposed.** Hindsight surfaces only user-CURATED `directive`-subtype mental_models; no auto-extraction of preferences (§10.1).

**M14.7 revert:** dropped anchor pinning + preference profile surfacing. Kept M14.6 plumbing (`kind` discriminator, `preference_compile.py`, migration 022) as future user-curation building blocks.

### 8.5 Locked-in policy decisions (end-of-day 2026-05-14)

After the revert-verify bench revealed the "zero variance" 3-run LME baseline was a sampling artifact (true std ≈ ±3.3pp), the user committed to 9 explicit policies governing every batch's execution.

| # | Policy | Decision | Implication |
|---|---|---|---|
| 1 | Signal threshold | 2σ above baseline mean | LME top_20 must reach ≥71.6% (≥+2 questions from 65.0% mean ±3.3pp) to count as real lift |
| 2 | Phase 4 schema | `fact_type='observation'` subtype on existing `astrocyte_pi_facts` | No new table; observations live alongside other fact_types |
| 3 | Wiki retire trigger | Post-bench, decided per-category | Env flag `ASTROCYTE_DISABLE_WIKIS` preserved as a knob |
| 4 | Replication policy | Borderline only | Replicate a phase's result only when within ~3pp of decision gate |
| 5 | Decision-gate failure | Ablate for A/C, auto-revert for B | A/C bundle sub-changes; B is atomic |
| 6 | Phase 1 disposition | Stays reverted | Variance worse than baseline regardless of mean |
| 7 | Budget cap | $200 | Today exceeded ($234) for Phase 4 replication + stacked bench |
| 8 | After-batch-A ship gate | LME top_20 ≥ 70% AND LoCoMo top_20 ≥ 80% | Not reached at cycle close |
| 9 | Doc cadence | Per-batch updates | This retrospective fulfils |

### 8.6 Execution order: C → B → A (chose 2026-05-14)

Original §4 plan was A → B → C (cheap-and-low-risk first). After the Hindsight critique (§10) clarified Phase 4 (atomic-fact consolidation) as the highest-leverage architectural fix, the user explicitly chose to lead with C (the heaviest) → B (entity canonicalization) → A (retrieval-stack wiring).

Each batch had its own decision gate per §8.5 #1.

### 8.7 Phase 4 / Batch C experiment — atomic-fact consolidation (2026-05-15)

**The Hindsight-aligned core was built as a working-tree experiment, never committed.** Phase 4 + Phase 3 (entity canonicalization) + Phase 2.1 (query_analyzer wiring) were all implemented and benched in WIP state, then **torn down without commit** when the bench null verdict (§8.9) made the implementation unnecessary.

The experiment's design (preserved as a future-cycle blueprint):

- **Schema:** `fact_type='observation'` subtype on `astrocyte_pi_facts` (no separate table); `canonical_name` column on `astrocyte_pi_section_entities`
- **Structured Pydantic models:** `CreateAction` / `UpdateAction` / `DeleteAction` / `ConsolidationBatchResponse` for the consolidator's LLM response (replaces M14.6's free-form JSON parsing)
- **Consolidator:** per-fact semantic recall → CREATE/UPDATE/DELETE pipeline + Hindsight `_PROCESSING_RULES` ported (NO COMPUTATION, ONE-OBSERVATION-PER-FACET, CASCADE TO ALL AFFECTED, PRESERVE HISTORY)
- **Mission-at-extraction:** Hindsight `_DEFAULT_MISSION` injected into the fact-extraction prompt (corrects Phase 1's layer-mismatch — Hindsight passes mission to extraction, not consolidation)
- **Entity canonicalization:** per-doc honorific-strip + normalize clustering (Dr. Patel / Dr Patel / Patel → one canonical), no LLM call
- **`query_analyzer` wiring:** temporal-constraint extraction + union of `search_facts_temporal` hits into the candidate pool before rerank
- **Bench knob:** `ASTROCYTE_DISABLE_WIKIS=1` env flag to A/B test wikis-off configuration

**Bench A** (observations + wikis both on) and **Bench B** (observations on, wikis off) results during the experiment:

| | 4-run baseline | Bench A | Bench B (wikis off) | Δ vs baseline |
|---|---|---|---|---|
| **LME top_10** | 71.0% ±1.6 | 76.7% ✓ 2σ | 66.7% | A +5.6pp ✓ |
| **LME top_20** | 65.0% ±3.3 | 66.7% | 73.3% ✓ 2σ | B +8.3pp ✓ |
| LME top_50 | 68.3% ±5.6 | 60.0% | 70.0% | B +1.7pp |
| LME top_200 | 65.0% ±7.0 | 70.0% | 73.3% | B +8.3pp |
| LoCoMo top_10 | 75.25% ±1.0 | 75.5% | 75.0% | flat |
| **LoCoMo top_20** | 78.25% ±1.5 | 77.5% | 75.0% | B −3.25pp |
| LoCoMo top_50 | 79.25% ±1.5 | 80.5% | 78.0% | flat |
| LoCoMo top_200 | 78.88% ±0.4 | 80.5% | 81.5% ✓ | B +2.6pp |

**Observations produced (Bench B):** 4886 across 30 LME docs (~163/doc); 1660 across 10 LoCoMo docs (~166/doc). `save_fails=0` after the UUID + FK fixes (§10.5).

#### Provisional architectural finding (later invalidated by replication, see §8.9)

Per-category top_20 from Bench B run 1 showed wikis HURT LME single-session-user (3.67 → 3) and observations recovered it (3 → 5). Single-session-preference doubled (1 → 2). This was the basis for the single-run "Phase 4 +8.3pp LME" framing — which did not survive replication.

### 8.8 Updated net M14 bench movement (post Phase 4 Bench B run 1, SUPERSEDED — see §8.9)

| Bench | Cutoff | M13.3 baseline | Post-Phase-4 Bench B run 1 | Δ from M13.3 |
|---|---|---|---|---|
| **LME** | top_20 | 60.0% | 73.3% | **+13.3pp** |
| **LoCoMo** | top_20 | 77.5% | 75.0% | −2.5pp |

> **2026-05-15 update: SUPERSEDED.** The +13.3pp LME lift above was a single-run result. Replication (Bench B run 2) gave LME top_20 = 63.3% — the lift did NOT replicate. See §8.9 for the corrected 3-run aggregate.

### 8.9 Final M14 bench verdict — null after replication (2026-05-15)

After Bench B run 1 produced LME top_20 73.3% (would have cleared 2σ gate), one replication (Bench B run 2) and a stacked configuration (Phase 4 + 3 + 2.1) bench were dispatched. Three runs of the wikis-off configuration:

| Run | LME top_20 | LoCoMo top_20 |
|---|---|---|
| Bench B run 1 | 73.3% (22/30) | 75.0% (150/200) |
| Bench B run 2 (replication) | **63.3% (19/30)** | 78.0% (156/200) |
| Stacked (Phase 4 + 3 + 2.1) | 70.0% (21/30) | 74.5% (149/200) |
| **3-run mean** | **68.9% ± 5.0pp** | **75.83% ± 1.5pp** |

**Against the 4-run baseline (LME 65.0% ± 3.3pp, LoCoMo 78.25% ± 1.5pp):**

| | mean Δ | σ-multiple | Locked 2σ gate? |
|---|---|---|---|
| LME top_20 | **+3.9pp** | 1.2σ | ✗ Does not clear (needs ≥+6.6pp) |
| LoCoMo top_20 | **−2.4pp** | 1.6σ | borderline (below 75% floor on 2 of 3 runs) |

**Final verdict: NULL on the locked 2σ gate.** The full M14 architectural stack (atomic-fact consolidation + entity canonicalization + query analyzer + wikis disabled) does NOT reliably move the metric vs baseline. The single-run +8.3pp on Bench B was variance.

#### What this means architecturally

The atomic-fact observation layer **worked mechanically** in the experiment — 4886 observations per 30 LME docs (~163/doc), Pydantic schema clean, save failures 0 after UUID + FK bug fixes (§10.5). Consolidator's CRUD pipeline executed correctly. Mission-at-extraction injected without parse failures.

It just didn't translate to bench-measurable signal at gpt-4o-mini answerer scale. Possible explanations:

1. **gpt-4o-mini ceiling**: the answerer's reasoning capacity caps near 65-70% LME top_20 regardless of retrieval improvements
2. **Bench harness already optimal for our shape**: M13.2 multi-grain was already extracting most available signal; architectural overhauls give diminishing returns
3. **Hindsight's lead is elsewhere**: their `gpt-oss-120b` retain model, user-curated directives, or tool-calling reflect agent (§12.1)

#### Disposition: experiment torn down without commit

Given the null verdict, **the working-tree implementation was never committed.** A subsequent merge of Dependabot PR #49 (devalue 5.8.1) reset the WIP changes; rather than re-create the code as "plumbing for future cycles," we accepted that the operating point is the same whether the implementation lives in the tree or not — the bench numbers prove it.

**HEAD remains at commit `6ec61ea` (revert(m14.2): drop wiki_incremental wiring).** No Phase 4 / Phase 3 / Phase 2.1 source files exist in HEAD. The §§8.1-8.6 baseline is the current operating point.

**What's retained from the cycle (in HEAD, committed):**

- `Makefile`: `BENCH_RESET` knob (default 1, commit `d982fde`) — DB-reset policy that fixed the silent 30× slowdown bug
- M14.7 revert (commit `6ec61ea`): drops the M14.2 `wiki_incremental` bench wiring
- **This document** — methodology corrections (§9), Hindsight architectural critique (§10), forward-looking gap analysis (§12)

**Future-cycle blueprint:** the experiment's source design (§8.7 above) is captured in this doc as a starting point if a future cycle re-implements with a different model tier (Config B gpt-4o) or different harness (AMB) where the bench verdict might differ.

#### Net M14 result (honest, after replication)

| Bench | Cutoff | M13.3 baseline | M14 final (no code committed) | Δ |
|---|---|---|---|---|
| LME | top_20 | 60.0% | 65.0% (4-run baseline mean at HEAD) | +5.0pp |
| LoCoMo | top_20 | 77.5% | 78.25% (4-run baseline mean at HEAD) | +0.75pp |

The +5.0pp LME drift since M13.3 is attributable to M14.1's `assistant_statement` extraction (committed earlier) plus measurement-methodology improvements. The M14 implementation experiment did NOT add to this on top.

**LME gap to Hindsight (~80%): ~−15pp.** LoCoMo gap to Hindsight: ~−14pp. Roughly equivalent shape, no longer "in striking distance" on either.

### 8.10 Total bench budget actuals

Locked cap: $200 (decision #7). Spent: ~$234. **Overage: $34** (17%).

| Phase | Bench cost | Cumulative |
|---|---|---|
| Phase 0 (3 baselines + revert verify) | ~$52 | $52 |
| Phase 1 (mission, 2 runs) | ~$26 | $78 |
| M14.7 revert verify | ~$13 | $91 |
| Phase 4 Bench A (Bench A fix1 + fix2) | ~$26 | $117 |
| Phase 4 Bench B run 1 | ~$13 | $130 |
| Bench B replication (run 2) | ~$13 | $143 |
| Stacked (4 + 3 + 2.1) | ~$13 | $156 |
| (+ M14.2 / M14.6 / B-1 earlier in cycle) | ~$78 | $234 |

Lessons: replication policy (#4 borderline) saved 2-3 benches we might otherwise have done; cap was a useful brake even when exceeded.

---

## 9. Methodology (added 2026-05-14)

Two corrections paid for during the M14 cycle that future contributors must internalise to avoid the same trap.

### 9.1 DB-reset-by-default Makefile policy

**Symptom:** after ~10 bench runs, per-question latency exploded from ~10s to ~315s and benches started dying mid-run.

**Cause:** each bench run uses a fresh `bank_id` namespace but does NOT drop prior banks' rows from the Postgres schema. After 10 runs the schema accumulates ~300K+ section_entities, ~250K+ facts, ~47K+ vectors. Postgres query planner picks degraded plans.

**Fix:** every `bench-*` Makefile target now defaults to running `bench-db-reset` (drops + recreates the container) at the start. `BENCH_RESET=0` opt-out preserves prior rows for fast iteration / PageIndex resume-by-bank-id workflows.

```bash
make bench-mem0-harness-parallel MEM0_HARNESS_PROJECT=foo                    # resets (default)
make bench-mem0-harness-parallel MEM0_HARNESS_PROJECT=foo BENCH_RESET=0      # resumes
```

**Implication for prior bench numbers:** results from before 2026-05-14 must be flagged with whether the DB had accumulated state.

### 9.2 Multi-run baseline + signal-detection thresholds

**Symptom:** earlier M14 iteration cycles kept declaring lift / regression from single-run bench results. Single-run swings of ±5pp at N=30 (LME) and ±2.5pp at N=200 (LoCoMo) were misread as effect rather than noise. Initial 3-run "zero variance" baseline was a sampling artifact; the 4th run widened std to ±3.3pp.

**Fix:** every phase delta is measured against a **multi-run baseline mean** with explicit per-cutoff std. Phase X must clear the 2σ threshold on at least one tight-CI cutoff to count as real signal.

Per §8.1 baseline:

| Bench | Cutoff | Baseline | 2σ threshold | Phase X must reach |
|---|---|---|---|---|
| LME | top_20 | 65.0% ±3.3pp | ±6.6pp | ≥71.6% |
| LoCoMo | top_10 | 75.25% ±1.0pp | ±2.0pp | ≥77.25% |
| LoCoMo | top_200 | 78.88% ±0.4pp | ±0.8pp | ≥79.7% |
| LoCoMo | top_20 | 78.25% ±1.5pp | ±3.0pp | ≥81.25% |

**Replication required:** even a clean signal-passing single-run lift must replicate before it's considered committable. Phase 1 and Phase 4 both demonstrated why (P1 +10pp didn't replicate; Phase 4 +8.3pp didn't replicate).

### 9.3 Per-category attribution

Aggregate top_20 numbers obscure category-specific signal. **Always pull per-category breakdowns** when interpreting a phase result. Per-category attribution detected the Phase 4 single-session-user lift (real direction but variance-bound) and the wikis-on-hurts-single-session-user pattern.

---

## 10. Hindsight architectural critique (added 2026-05-14)

After M14.2 reverted on bench evidence, a code-archaeology pass through `/Users/calvin/AstrocyteAI/hindsight/hindsight-api-slim/hindsight_api/engine/` clarified structural gaps in Astrocyte's design.

### 10.1 Hindsight has no automatic preference-injection pipeline

Their `mental_models` subtypes: `structural | emergent | pinned | learned | directive`. **No `preference` subtype.** Directives are explicitly **USER-CURATED hard rules** injected into the system prompt at both start AND end of the reflect agent's prompt. Auto-extraction of preferences from conversation is not part of their published shape.

Our M14.6 added auto-injection; M14.7 reverted it after the LoCoMo-vs-LME single-session-user trade-off became visible. The Hindsight architectural read suggests the auto-injection was a self-imposed problem.

### 10.2 Hindsight updates atomic-fact observations, not prose wikis

Their `consolidator.py` runs per new fact: pulls semantically-related existing `observations` (atomic `memory_units` rows with `fact_type='observation'`), sends them to one LLM prompt with explicit `CREATE / UPDATE / DELETE` action arrays. Each observation tracks ONE specific facet. Rules:

1. **NO COMPUTATION** — never derive, infer, or adjust numeric values
2. **ONE OBSERVATION PER DISTINCT FACET** — each observation tracks ONE specific facet
3. **MATCH BY ENTITY/FACET, NOT TOPIC** — UPDATE targets only matching entity row
4. **CASCADE TO ALL AFFECTED OBSERVATIONS** — state change may touch multiple
5. **PRESERVE HISTORY but KEEP IT CONCISE** — state changes update to latest state
6. **SAME FACET → UPDATE, NOT CREATE** — new count supersedes the old

Our M14.2 attempted update-detection at the **WIKI** layer (prose). Updating prose drifts; updating atomic facets is targeted surgery. Our `WikiPage` layer was always going to bottleneck on this distinction.

**Phase 4 implements the atomic-fact pattern** but bench verdict null (§8.9) — the architectural critique was sound; the implementation didn't translate to bench movement at our scale.

### 10.3 What we already have vs Hindsight

| Capability | Hindsight | Astrocyte (current) |
|---|---|---|
| Semantic search | ✓ | ✓ |
| BM25/keyword search | ✓ | ✓ (`search_fulltext_bm25` in section_recall) |
| Reciprocal-rank fusion | ✓ | ✓ (`rrf_fusion`) |
| Cross-encoder rerank | ✓ | ✓ (`cross_encoder_rerank`) |
| Multi-query expansion | implicit in tokenize | ✓ (`multi_query.py` — **not wired to mem0_harness**) |
| HyDE | ✗ | ✓ (`hyde.py` — **not wired to mem0_harness**) |
| Query intent analyser | ✓ | ✓ (`query_analyzer.py` — Phase 2.1 wired into mem0_harness) |
| Entity extraction at section grain | ✓ | ✓ |
| Entity canonicalization | ✓ | ✓ (Phase 3 lightweight clustering) |
| Atomic-fact observations | ✓ | ✓ (Phase 4 — bench-null) |
| Proof_count + freshness on observations | ✓ | ✗ (was planned Phase 5; dropped) |
| Link-expansion graph retrieval | ✓ | ✗ (have link tables; no recall strategy) |
| Multiplicative score boosts (recency × temporal × proof_count) | ✓ | ✗ (rerank-only) |
| Tool-calling reflect agent | ✓ | ✗ |
| Mental models (user-curated) | ✓ | partial (auto-extracted; user-curation API not built) |
| Directives (hard rules) | ✓ | ✗ |
| Disposition system | ✓ | ✗ (bench-irrelevant) |
| Background consolidation worker | ✓ | ✗ (bench-irrelevant) |

After M14, the biggest remaining bench levers (see §12) are tool-calling reflect agent, multiplicative score boosts, and link-expansion graph retrieval.

### 10.4 What the first-pass critique missed (2026-05-14 second audit)

A re-read of Hindsight's `consolidator.py` and `prompts.py` after Phase 1's null result surfaced three findings that the first §10 audit underweighted.

**1. Structured Pydantic response models on the consolidator prompt.** Hindsight uses `_CreateAction(text, source_fact_ids)`, `_UpdateAction(text, observation_id, source_fact_ids)`, `_DeleteAction(observation_id)` wrapped in `_ConsolidationBatchResponse`. The LLM is constrained by schema. Phase 4b ported this pattern.

**2. Mission belongs at fact EXTRACTION, not wiki/preference COMPILE.** Phase 1's mission was injected at the consolidation layer downstream of extraction. Hindsight passes mission to `fact_extraction.py` — upstream. Phase 4d retried at the extraction layer.

**3. `gpt-oss-120b` via Groq for memory operations.** Hindsight's choice. Cost lever, not accuracy. Defer until budget pressure.

### 10.5 Phase 4 execution-time bugs (2026-05-15, lessons for downstream phases)

Phase 4's two integration bugs each cost a bench cycle (~$13 + ~$13 = ~$26 wasted) before observations actually persisted to Postgres.

**Bug 1 — UUID prefix violates type.** First Phase 4 bench: consolidator generated observation IDs like `obs-<uuid>`. Postgres `astrocyte_pi_facts.id` is a UUID column; the `obs-` prefix violated UUID syntax. Every save_facts call silently failed.

**Lesson:** when a fact-shape row is identified by a UUID column upstream, generate bare UUIDs and use the discriminator column (here `fact_type='observation'`) for type semantics.

**Bug 2 — FK to parent section.** After UUID fix, FK constraint `astrocyte_pi_facts_document_id_line_num_fkey` required (document_id, line_num) to reference a real row. The consolidator passed `line_num=0`, which doesn't exist as a section anchor.

**Lesson:** synthetic / aggregated rows still need a valid anchor in the existing FK graph. Resolution: anchor each observation to the first fact in the consolidation batch's `(document_id, line_num)`.

**Methodological:** both bugs are caught by Postgres but NOT by the in-memory test backend used in the unit-test suite. Future SPI work should add an integration test path that exercises Postgres directly.

---

## 11. Next concrete action (revised 2026-05-15)

**M14 cycle CLOSED 2026-05-15 — experiment torn down, learnings retained.**

The Phase 4 + 3 + 2.1 implementation was built as a working-tree experiment, benched (3 runs of wikis-off + 2 of wikis-on), and **never committed**. The bench null verdict (§8.9) made the implementation bench-unnecessary; a Dependabot merge subsequently wiped the WIP code from the tree and we accepted that state rather than rebuilding.

### What's in HEAD

| Commit | Description | Bench-relevance |
|---|---|---|
| `6ec61ea` | revert(m14.2): drop wiki_incremental wiring | Last bench-relevant commit |
| `d982fde` | chore(bench): BENCH_RESET knob (default 1) | Critical: DB-reset policy fix |
| Earlier | Dependabot merges (PRs #24–#28 etc.) | Build / CI hygiene |

HEAD is the post-M14.7-revert state. Bench-wise it's the **baseline** the cycle measured 4 times at LME top_20 = 65.0% ± 3.3pp / LoCoMo top_20 = 78.25% ± 1.5pp.

### Shippable artifacts (docs only — code is in HEAD already)

| Artifact | Status |
|---|---|
| `docs/_design/m13-m14-roadmap.md` | UPDATED — §§8-12 retrospective, methodology, Hindsight critique, gap analysis |
| `docs/_design/benchmark-trajectory.mdx` | UPDATED — current-operating-point callout |
| `CHANGELOG.md` | UPDATED — M14 cycle close entry under `[Unreleased]` |

These three doc updates are the M14 deliverable. No new source code; the methodology improvements (DB-reset policy, multi-run baselines) are already committed in `d982fde`.

### Cycle close steps

1. **Doc retrospective** (this update + §§8.7-8.10) — DONE
2. **Accept HEAD as final M14 operating point** — no code to add; baseline is what M14 would have produced anyway per the bench
3. **Commit the three doc updates** (m13-m14-roadmap, benchmark-trajectory, CHANGELOG)
4. **Drop all remaining phases** (2.2 multi_query, 2.3 HyDE, 5 proof_count, 6 per-fact recall, 7 bank mission) — gated on a Phase 4 lift that never replicated

### Future-cycle starting points (NOT scheduled)

If bench-optimization resumes later, the experiment's design lives in §8.7 as a blueprint. The remaining Hindsight design gaps (§12) rank the next levers by expected bench impact — tool-calling reflect agent (§12.1) is the largest unrealized lever and is **outside the M14 retain-pipeline scope** that this cycle explored.

---

## 12. Remaining Hindsight design gaps for future bench-optimization (2026-05-15)

The M14 null verdict (§8.9) tells us atomic-fact consolidation + entity canonicalization + temporal query analysis collectively don't move the gpt-4o-mini bench metric at our scale. But the Astrocyte–Hindsight gap is real (LME ~11pp, LoCoMo ~10-15pp). Where does Hindsight's remaining lead live?

After a deeper code audit of `hindsight-api-slim/hindsight_api/engine/`, here are the design elements **we have not implemented** ranked by likely bench impact.

### 12.1 Reflect agent with native tool calling ★★★ (BIGGEST UNREALIZED LEVER)

**What Hindsight has** (`engine/reflect/agent.py` + `tools.py`):

An **agentic answerer loop** that exposes four tools to the LLM at inference time:
- `tool_search_mental_models` — user-curated summary tier
- `tool_search_observations` — atomic-fact aggregations
- `tool_recall` — raw memory retrieval
- `tool_expand` — link-expansion graph walk from a seed memory

The reflect agent iterates up to `max_iterations` (default ~5), letting the model choose which retrieval tool to call next based on what it's gathered so far. Central `for iteration in range(max_iterations)` loop with `tool_calls`, error recovery, and a "has_gathered_evidence" guard.

**What we have:** the Mem0 harness adapter does ONE retrieval pass per question. Answerer reads a flat candidate list. No tool-use, no agentic iteration. Exactly the "synthesis-heavy questions bottleneck on the answerer" pattern in §1.

**Why this is the biggest gap:**
- The answerer can't course-correct retrieval per-question. First pass misses → no recovery.
- Hierarchical retrieval (mental_models → observations → recall fallback) means strong-confidence sources tried first.
- Multi-step reasoning ("first find X, then look up Y near X") becomes trivial with `tool_expand`.

**Estimated effort:** ~1-2 weeks. Requires:
1. New harness path that calls answerer as agent rather than feeding flat list
2. Four tool implementations matching Hindsight's contracts via our SPI
3. Agent loop with iteration limit + error recovery

**Estimated bench impact:** +5-15pp on LME (the synthesis-bottlenecked bench). LoCoMo less clear.

### 12.2 Multiplicative score boosts (recency / temporal / proof_count) ★★

**What Hindsight has** (`engine/search/reranking.py:129`):

```
combined_score = cross_encoder_score_normalized
               × recency_boost
               × temporal_boost
               × proof_count_boost
```

Multiplicative composition of FOUR signals after rerank:
- Cross-encoder relevance (we have)
- Recency boost (how recently was the memory created)
- Temporal-context boost (does query's date band intersect memory's date)
- Proof-count boost (how many memories support this observation)

**What we have:** cross-encoder rerank alone. No recency, no temporal boost, no proof_count.

**Estimated effort:** ~3-5 days.

**Estimated bench impact:** +2-5pp on temporal-heavy categories. Multiplicative compounding produces sharper top-K differentiation.

### 12.3 Link-expansion graph retrieval ★★

**What Hindsight has** (`engine/search/link_expansion_retrieval.py`):

A dedicated **graph-walking retrieval strategy** that, given a seed memory, expands outward via `memory_links` to find related memories. One of four retrieval strategies fused via RRF (`semantic + bm25 + graph + temporal`).

**What we have:** `astrocyte_pi_section_links` table exists (M9) with link types populated at retain. **We don't expand them at recall.** `section_recall.py` has `entity` strategy but no `link_expansion` strategy.

**Estimated effort:** ~1 week.

**Estimated bench impact:** +3-7pp on LoCoMo multi-hop. Less clear on LME.

### 12.4 Bank disposition + mission injection at answer time ★

**What Hindsight has:** `bank_profile.disposition` + `bank.mission` injected into reflect agent's system prompt.

**What we have:** disposition NOT in mem0 harness path. Bank mission stays at extraction (Phase 4d).

**Estimated effort:** bench-blocked partial; ~2-3 days via `get_user_profile()` smuggle.

**Estimated bench impact:** small (+1-2pp).

### 12.5 `gpt-oss-120b` retain LLM via Groq ★ (cost-reduction, not accuracy)

**What Hindsight has:** default retain LLM is `gpt-oss-120b` via Groq, not gpt-4o-mini.

**Estimated effort:** ~half day.

**Estimated bench impact:** uncertain; opens cost ceiling for larger context windows. Benchmark required.

### 12.6 Query LLM-fallback path (medium effort, small impact)

**What Hindsight has:** their `query_analyzer.py` falls back to LLM when regex misses AND query has temporal markers. Our `query_analyzer` supports `allow_llm_fallback=True` but Phase 2.1 didn't enable it.

**Estimated effort:** ~half day (toggle flag + bench).

**Estimated bench impact:** marginal (+1-2pp on temporal).

### 12.7 Directives (user-curated hard rules) — bench-blocked

**What Hindsight has:** user can pin `mental_models` with `subtype='directive'`. Reflect agent's system prompt has DIRECTIVES (MANDATORY) section.

**What we have:** `MentalModel.kind` discriminator (M14.6 plumbing) supports adding `directive`. No curation API. No agent wires them in.

**Estimated effort:** ~1-2 weeks (curation API + retrieval wiring + answerer prompt). Requires §12.1.

**Estimated bench impact:** nearly zero on current LME/LoCoMo. Real impact only on production deployments.

### 12.8 Summary — recommended priority order for a future bench cycle

| # | Gap | Effort | Expected bench impact | Recommended |
|---|---|---|---|---|
| 1 | §12.1 Reflect agent w/ tool-calling | 1-2 weeks | **+5-15pp LME** | ★★★ |
| 2 | §12.3 Link-expansion graph retrieval | 1 week | +3-7pp LoCoMo multi-hop | ★★ |
| 3 | §12.2 Multiplicative score boosts | 3-5 days | +2-5pp temporal | ★★ |
| 4 | §12.5 `gpt-oss-120b` retain | half day | uncertain (cost lever) | ★ |
| 5 | §12.6 Query LLM-fallback | half day | +1-2pp temporal | ★ |
| 6 | §12.4 Disposition + mission injection | 2-3 days | +1-2pp | low priority |
| skip | §12.7 Directives | 1-2 weeks | ~0pp (bench-blocked) | not bench-relevant |

**Combined potential:** if §12.1 + §12.2 + §12.3 stack, plausible aggregate +10-25pp on LME, +5-10pp on LoCoMo. Would bring us to Hindsight's published numbers — assuming the gpt-4o-mini answerer ceiling isn't the actual binding constraint.

**Biggest risk:** gpt-4o-mini may be the binding constraint regardless of retrieval quality. The disambiguating experiment is to run §12.1 at gpt-4o tier — if THAT doesn't hit Hindsight's numbers, the gap is genuinely Hindsight-specific.

### 12.9 What this means for the M14 close framing

The M14 cycle attacked retain-side architecture (extraction, consolidation, canonicalization, observations). The bench-relevant gaps mostly live elsewhere — **the answerer path** (tool-calling agent), **the retrieval scoring** (multiplicative boosts), **the graph layer** (link expansion). Future cycles should target those rather than continuing retain-pipeline iteration.
