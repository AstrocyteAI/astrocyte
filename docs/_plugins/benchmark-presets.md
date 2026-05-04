---
title: Benchmark presets and the v1 default
---

# Benchmark presets and the v1 default

Astrocyte ships **fast-recall** as the v1 default benchmark configuration
(`benchmarks/config.yaml`), selected after the five-preset ablation matrix
described below. This page explains the trade-offs each preset embodies, the
empirical results that drove the v1 decision, and what is deliberately
deferred for v1.1.

## The five presets

Each preset is a complete config under `benchmarks/`. They share the
Postgres + pgvector + AGE + OpenAI stack and differ only in the retrieval
and reasoning bundles enabled. The ablation matrix
(`benchmarks/ablation-matrix.json`) registers each scenario plus its
promotion gates.

| Preset | Bundle highlights |
|---|---|
| `baseline` | Tier-1 storage only, no agentic-reflect, no cross-encoder, no SFE, no semantic/causal links, no adversarial defense |
| `fast-recall` | Adds spreading_activation + semantic_link_graph + SFE-verbatim + cheap adversarial defense (abstention floor + adversarial prompt) + query_analyzer. No agentic-reflect, no cross-encoder, no causal links, no premise verification |
| `hindsight-parity` | The full Hindsight-inspired bundle: agentic-reflect, cross-encoder rerank, causal links, larger candidate pools, extraction-discipline prompt. No adversarial defense |
| `hindsight-balanced` | hindsight-parity + the cheap adversarial defense subset from fast-recall (single-variable ablation test) |
| `quality-max` | Upper-bound experiment: hindsight-parity + premise verification + LLM entity disambiguation + larger pools + larger token budgets |

## LoCoMo results — 2026-05-03 fair-bench (200 questions, 10 conversations, BM25 keyword search active)

| Preset | Overall | adversarial | single-hop | multi-hop | open-domain | temporal | $/q |
|---|---|---|---|---|---|---|---|
| baseline | 43.5% | 0.683 | 0.634 | 0.342 | 0.194 | 0.293 | $0.0025 |
| **fast-recall** ⭐ | **51.5%** | **0.707** | 0.780 | 0.439 | 0.194 | 0.415 | **$0.0035** |
| hindsight-parity | 50.5% | 0.366 | **0.854** | 0.537 | 0.306 | 0.439 | $0.0085 |
| hindsight-balanced | 49.0% | 0.488 | 0.756 | 0.512 | **0.333** | 0.341 | $0.0076 |
| quality-max | 42.5% | 0.659 | 0.585 | 0.366 | 0.139 | 0.341 | $0.0072 |

Notes:
- `fast-recall` is the Pareto winner — best overall accuracy at the lowest cost
- `hindsight-parity` wins single-hop and is best on synthesis (only 3/200 cases of "had recall, missed synth"), but loses 34pt on adversarial because the extraction-discipline prompt suppresses abstention
- `hindsight-balanced` was the single-variable test of "cheap adversarial defense added to hindsight-parity." It recovered ~half the adversarial points (+12pt) but cost 10pt on single-hop and 10pt on temporal — net regression to 49.0%
- `quality-max` proved that more features is not more accuracy: stacking premise verification, LLM disambiguation, and larger pools collapsed it to 42.5%

## Why fast-recall is v1

1. **Pareto-optimal at the current capability frontier.** It beats every other preset on overall accuracy. Hindsight-parity's per-category wins on single-hop / multi-hop / open-domain do not translate to overall accuracy because they are paid for with adversarial losses
2. **Cheapest by a factor of 2-3x** ($0.0035/q vs $0.0072–$0.0085 for the heavier presets). Important for both bench iteration and production economics
3. **Fastest end-to-end** (e2e p95 ~23s vs ~69-103s for the agentic presets) because there is no agentic-reflect loop and no cross-encoder
4. **Predictable behavior.** No agentic loop means the answer trace is short, making failure-mode analysis easy. The quality-max collapse showed how much harder the heavier presets are to debug

## What's deferred for v1.1

The matrix made several limitations explicit. None blocks v1, all are worth pursuing:

- **Conditional abstention.** The flat 0.2 abstention floor over-fires on
  legitimate single-hop and temporal questions whose top semantic similarity
  scores below 0.2. A query-intent-aware gate (apply the floor only when
  intent classifier flags adversarial) would likely lift fast-recall further
- **BM25-vs-temporal trade-off.** Adding BM25 to fast-recall lifted multi-hop
  (+4.9pt) and open-domain (+5.6pt) but cost temporal (-7.3pt) and
  single-hop (-2.4pt). The lexical strategy displaces temporal-decay slots in
  RRF fusion. A per-intent strategy weighting would address this
- **Latency floor.** Even baseline shows recall p95 ~11s. The new
  `strategy_timings_ms` instrumentation in `RecallTrace` should bisect the
  long pole — likely temporal scan or pgvector cold pool. The 2000ms gate in
  `gates-baseline.json` is aspirational; v1 ships at ~14s p95
- **Agentic stack as research preset.** `hindsight-parity` retains the best
  synthesis quality (3/200 synth-misses). Available as
  `--config benchmarks/config-hindsight-parity.yaml` for runs where adversarial
  is not on the scoreboard
- **Hindsight published 92% on LoCoMo.** Astrocyte v1 ships at 51.5%. The
  gap is not a single preset choice; it is a long roadmap of investments
  documented in the matrix

## Why abstention_floor was dropped (post-mortem)

The `adversarial_defense.abstention_floor` knob had two failed iterations
before we removed it from `hindsight-balanced`. Documenting both so this
isn't re-litigated.

### Iteration 1: flat 0.2 floor on `hindsight-balanced`

Hypothesis: a score-floor short-circuit would lift adversarial accuracy
without harming the bundle's wins on other categories.

| Preset | Overall | adversarial | single-hop | temporal |
|---|---|---|---|---|
| hindsight-parity (no floor) | 50.5% | 0.366 | 0.854 | 0.439 |
| hindsight-balanced (flat 0.2 floor) | **49.0%** ⚠ | 0.488 (+12pt) | 0.756 (−10pt) | 0.341 (−10pt) |
| fast-recall (flat 0.2 floor + simpler stack) | **51.5%** | **0.707** | 0.780 | 0.415 |

The flat floor recovered ~12 adversarial points but dropped 10pt on
single-hop AND 10pt on temporal. Net regression. The mechanism: legitimate
single-hop and temporal queries occasionally have top retrieval scores
below 0.2 (paraphrasing distance, embedding noise), and the flat floor
short-circuits them to "insufficient evidence" before the LLM gets a
chance.

### Iteration 2: intent-conditional floor (`abstention_floor_intent_only`)

Hypothesis: the existing query-intent classifier could distinguish
adversarial from legitimate questions, so we could fire the floor only
for non-well-formed intents.

Result: **47.0% overall** (worse than flat floor). The logs showed `0`
floor fires across the entire 200-question bench. Reason: every LoCoMo
question parses as a well-formed intent (FACTUAL/TEMPORAL/etc.) because
the intent classifier was designed for retrieval-bias selection, not
adversarial detection. The gate effectively disabled the floor entirely,
making `hindsight-balanced` functionally equivalent to `hindsight-parity`
EXCEPT that `adversarial_prompt_enabled: true` competed with the
extraction-discipline prompt rules — synthesis quality dropped (3 → 13
"recall hit, synth miss" cases) without any compensating gain.

### Iteration 3 (considered, NOT built): adversarial-shape detector

We sampled 446 real LoCoMo cat-5 (adversarial) questions and tested 14
candidate presupposition patterns. Empirical recall was **3.8%**: only 17
of 446 adversarial questions had detectable presupposition triggers (e.g.
"after X was promoted", "X's [business/store]", "overcome his injury").
The other 96% are lexically indistinguishable from legitimate factual
questions — the adversarial signal lives in **retrieval scores**, not
**query text**. A 3.8% recall ceiling means even a perfect detector
moves the bench by < 1pt. Not built.

### Conclusion

LoCoMo's adversarial accuracy is fundamentally a **retrieval-scoring
trade-off**, not a query-classification problem. fast-recall achieves
0.707 adversarial because the simpler stack (no agentic-reflect) means
the abstention floor doesn't compete with a multi-iteration synthesis
loop — when the floor fires, there's no second-chance reasoning to
override it. hindsight-balanced removed the floor entirely; its
adversarial accuracy is governed purely by the agentic loop + extraction
discipline + adversarial system-prompt rules.

If you want to revisit this in v1.x, the right axes are:

- **Multi-signal score gate** (top-1 + top-K mean + score variance)
  rather than a flat threshold
- **BM25 with proper IDF normalization** (Hindsight uses this; we don't)
  to improve baseline retrieval signal so the floor isn't needed
- **Abstention-rule injection in the agentic-reflect prompt** at the
  `done` tool boundary, not as a pre-LLM short-circuit

## How to bench against the matrix

Use the named presets via Make:

```bash
make bench-locomo-fair                                        # v1 default (= fast-recall)
make bench-locomo-fair CONFIG=benchmarks/config-hindsight-parity.yaml
make bench-locomo-fair CONFIG=benchmarks/config-quality-max.yaml
```

Or run the full matrix via the helper script
(`benchmarks/ablation-matrix.json` lists every scenario with its purpose).

## Where the receipts live

- Per-preset result files: `benchmark-results/results-matrix-<preset>.json`
  and `results-matrix-<preset>-fixed.json` (the latter group is post-`text_fts`
  fix in migration 011)
- The v1 decision is locked in by
  `tests/test_hindsight_informed_config.py::test_default_config_matches_fast_recall`
- The cross-preset trade-off discussion above should be revisited any time
  the default changes
