# M13 close-out and M14 retain pipeline overhaul — roadmap

**Status:** Living plan. M13.0–M13.2 landed; M13.3 close-out in flight at the date of this doc. M14 unstarted.

**Companion docs:**
- [benchmark-comparison-methodology.md](benchmark-comparison-methodology.md) — harness/judge variance principles
- [llm-wiki-compile.md](llm-wiki-compile.md) — Karpathy compile spec; M14 implements the parts we punted on
- [recall.md](recall.md) — current section + fact + wiki recall stack

---

## 1. Where we are

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

*This document is updated as milestones complete. When M13.3 closes, update §1 with the final M13.x numbers and check off the §7 actions. When each M14.x lands, update the corresponding row in §4 with actual effort vs estimate and actual bench lift vs projection.*
