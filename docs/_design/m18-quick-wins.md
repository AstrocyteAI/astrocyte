---
title: "M18 — Quick wins toward Hindsight bench parity"
draft: false
topic: design
---

# M18 — Quick wins toward Hindsight bench parity

**Status:** scoping (2026-05-17)
**Predecessor:** [M17 SHIPPED](./m17-pageindex-ingestion.md) — Conversation Engine cleared both ship gates at 3-run mean (LME +10.56pp, LoCoMo +2.25pp).
**Goal:** narrow the remaining gap to Hindsight's published numbers (LongMemEval 94.6%, LoCoMo 92.0%) with the smallest, highest-EV engine-side changes.

## 1. Current state vs target

| Bench | Astrocyte (M17 3-run mean) | Hindsight (published) | Gap |
|---|---|---|---|
| **LongMemEval top_20** | 75.56% | **94.6%** | **−19.04pp** |
| **LoCoMo top_20** | 80.50% | **92.0%** | **−11.50pp** |

M17 closed ~half the LME gap and a sliver of LoCoMo. M18 attacks the cheapest remaining levers.

## 2. Locked policy framework

| # | Decision | Locked value |
|---|---|---|
| 1 | Scope | TWO items: (a) §12.6 query_analyzer LLM-fallback flag flip + 3-run bench. (b) §12.2 multiplicative score boosts (recency × temporal × proof_count × cross_encoder) + 3-run bench. |
| 2 | Answerer model | gpt-4o-mini (apples-to-apples vs M17 baseline) |
| 3 | Judge model | gpt-4o-mini (same) |
| 4 | Budget cap | **$60 hard cap** (much smaller than M17's $400; both items are focused engine adds, not multi-phase architecture work). |
| 5 | Ship gate (per item) | **2σ over M17 3-run mean** as the new "baseline." Each item benched independently. |
| 6 | Halt gate | Any single LME run < 65% OR LoCoMo run < 76% ⇒ HALT that item, investigate before continuing. |
| 7 | DB reset policy | `BENCH_RESET=1` mandatory at every full bench run (carried forward from M17). |
| 8 | Baseline reference | LME 75.56% ±5.09pp / LoCoMo 80.50% ±1.50pp (M17 3-run mean, Conversation Engine path) |
| 9 | Replication N | 3 runs minimum per item; both items benched after each lands. |
| 10 | Branch | `feat/m18-quick-wins` (one branch, two sequential items) |
| 11 | Cycle-close policy | Each item independently ships-or-null-verdicts. Combined cycle-close documents both regardless of outcome. |

### Ship-gate arithmetic

| Bench | M17 reference | +2σ required | Required mean to ship each item |
|---|---|---|---|
| LME top_20 | 75.56% ±5.09pp | +10.18pp | **≥ 85.74%** |
| LoCoMo top_20 | 80.50% ±1.50pp | +3.00pp | **≥ 83.50%** |

Per-item gates. If item (a) lifts LME +2pp (within noise), it null-verdicts at this gate — but combined with item (b) might still clear when re-benched together at the end.

## 3. Item (a) — query_analyzer LLM-fallback flip

### 3.1 What changes

Single config flag flip in `astrocyte/config.py` `QueryAnalyzerConfig`:
```python
allow_llm_fallback: bool = True   # was False
```

The plumbing already exists end-to-end (M14):
- `astrocyte/pipeline/query_analyzer.py:331-399` — `analyze_query` already accepts the flag
- `astrocyte/pipeline/orchestrator.py:1998-2012` — call site already wired
- `astrocyte/_astrocyte.py:356` — config → pipeline wiring already exists

So this is a **zero-code-change item**. Just the default flag value.

### 3.2 Why it might lift bench

When a query's regex pre-pass misses a temporal constraint AND the query contains temporal-marker tokens (e.g., "last spring", "after my service", "around when I joined"), an LLM call extracts the date range. Currently these queries get NO temporal constraint extraction, so retrieval falls back to undated semantic search.

Expected impact:
- LME temporal-reasoning category (currently 80% at M17): possibly +5-15pp on this category alone
- Overall LME lift: **+1-2pp** (one category × 5/30 weight = ~+1.7pp ceiling)
- LoCoMo temporal category (currently 77.9% at M17): possibly +5-10pp
- Overall LoCoMo lift: **+1-2pp** similarly (86/200 weight)

### 3.3 Cost

- Per query: +1 LLM call (~$0.0001 at gpt-4o-mini) when regex misses + temporal markers present (~30% of queries)
- Bench cost per 3-run cycle: ~$5 (mostly the standard answerer + judge cost; the analyzer LLM call is rounding error)

### 3.4 Phases

| Phase | Description | Bench gate |
|---|---|---|
| (a)-1 | Flip default in `astrocyte/config.py`. Unit-test the flag propagation. | None |
| (a)-2 | 1-run LME-30 + LoCoMo-200 smoke (sanity) | within ±2σ of M17 3-run mean |
| (a)-3 | 3-run replication (parallel LME+LoCoMo) | LME ≥ 85.74% OR document the smaller-than-2σ-but-meaningful lift |

Total effort: ~1 day code + 3 bench runs (~$5).

## 4. Item (b) — multiplicative score boosts (§12.2)

### 4.1 What changes

Port Hindsight's reranking formula from `hindsight-api-slim/hindsight_api/engine/search/reranking.py:120-130`:

```python
combined_score = cross_encoder_score_normalized \
               × recency_boost \
               × temporal_boost \
               × proof_count_boost

# Each boost: 1.0 + alpha * (signal_normalized - 0.5)
# alpha values from Hindsight: typically 0.2 each (±10% per axis, ±21% combined)
```

Where:
- `recency_boost`: how recently the memory was created (already have `created_at`)
- `temporal_boost`: does the query's date band intersect the memory's date band (have `occurred_start/end`; need query date from query_analyzer — which item (a) lights up)
- `proof_count_boost`: how many memories support this fact (NEW column — needs a per-fact "supporting evidence count" tracked at retain time)

### 4.2 Why it WILL lift bench

Hindsight's blog explicitly attributes their performance to multiplicative composition over additive: "alpha boosts of 0.2 → ±10% swing each, combined ±21%". On their bench (94.6% LME), this is one of the headline differentiators.

Expected impact (per M14 doc §12.2):
- LME: **+2-5pp** broad (temporal categories disproportionately)
- LoCoMo: **+3-7pp** on temporal + multi-hop
- Synergy with item (a): query_analyzer feeds the temporal_boost; together they should compound

### 4.3 Cost

- No new LLM calls — pure scoring change at the rerank step
- Bench cost per 3-run cycle: ~$5
- Schema: 1 migration adding `proof_count INTEGER NOT NULL DEFAULT 0` to `astrocyte_pi_facts`

### 4.4 Phases

| Phase | Description | Bench gate |
|---|---|---|
| (b)-1 | Add `proof_count` column (migration 029); update fact insertion to compute it (count of facts citing the same entity within the same document). Unit tests. | None |
| (b)-2 | Port `reranking.py` formula. Replace existing cross-encoder rerank step. Add config knobs for the three α values (default to Hindsight's 0.2 each). | None |
| (b)-3 | 1-run LME-30 + LoCoMo-200 smoke | within ±2σ of M17 3-run mean |
| (b)-4 | 3-run replication | LME ≥ 85.74% AND LoCoMo ≥ 83.50% |
| (b)-5 | Combined (a)+(b) bench — both flags + boosts active | confirms compounding |

Total effort: ~3-5 days code + tests + 4-5 bench runs (~$10).

## 5. Open design decisions (resolve before item (b) implementation)

1. **`proof_count` semantics.** Hindsight's "proof count" is the number of memories supporting an observation. For us this could be:
   - (a) Per-fact: count of OTHER facts in the bank that share ≥1 entity AND are within the same `occurred_start/end` band
   - (b) Per-fact: count of facts citing the same entity, period (no temporal filter)
   - (c) Per-section: count of facts derived from this section (which already exists implicitly)
   - **Tentative: (b)** — simplest, doesn't require temporal joins at retain time. Update if Phase (b)-3 smoke shows poor signal.

2. **α tuning.** Hindsight ships with 0.2 each. Do we trust that for our LME/LoCoMo + smaller answerer model?
   - **Tentative: start at 0.2 each (Hindsight defaults). Phase (b)-4 may need a small grid search if 3-run mean is weak.**

3. **Recency normalization window.** `recency_boost` needs a window over which "recent" is defined. Hindsight uses a fixed time-decay. For LME/LoCoMo where conversations span weeks/months in synthetic time, the window matters.
   - **Tentative: use the bank's conversation timespan (latest − earliest) as the normalization window per query.**

4. **Compatibility with our existing rerank path.** Our `cross_encoder_rerank.py` produces raw scores; the multiplicative composition needs normalized scores. Need a normalization step before multiplication.
   - **Tentative: min-max normalize cross-encoder scores within the candidate batch, then apply boosts.**

## 6. Out of scope for M18

- **§12.3 link-expansion retrieval** — bigger architectural change; separate M19 cycle.
- **BM25 / keyword search strategy** — needs Postgres FTS setup, indexing, tuning; separate M20 cycle.
- **World vs Experience fact distinction** at extraction time — prompt + schema change; separate cycle.
- **Reflect agent revisit** — only valuable AFTER §12.2 + §12.3 land; future cycle.
- **Document Engine bench on FinanceBench / DoubleBench** — separate workstream entirely.

## 7. Cycle close criteria

The M18 cycle is closed when ALL of:

- Item (a) shipped or null-verdicted (3-run mean + decision documented)
- Item (b) shipped or null-verdicted (3-run mean + decision documented)
- If both shipped: combined (a)+(b) 3-run bench documents the compounded effect
- M18 cycle-close section written into THIS doc with final numbers
- New baseline established for M19 if any items shipped

## 8. Cycle close (TBD)

To be filled in at end of cycle.
