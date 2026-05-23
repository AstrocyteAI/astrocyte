# M34 — Query-intent routing + per-fact-type segmentation

**Status**: Design (2026-05-21)
**Cycle**: Folded into v0.15.0 (per user decision after v015j miss)
**Blocks ship**: Yes — LME top_50 ≥ 70.0% ship-gate currently missed by 3.3pp

## Problem

The v015i/v015j benches showed a structural pattern:

| Run | LME top_50 | LoCoMo top_50 | Mechanism |
|-----|------------|---------------|-----------|
| v015f (baseline) | **70.0%** | 84.5% | pre-M33-3, mentioned_at=0% |
| v015i (M33-3, no cap) | 66.7% | **86.5%** | mentioned_at=100% floods temporal channel |
| v015j (M33-3, cap=5) | 66.7% | 83.5% | cap shuffles which categories win/lose |

Both M33-3 attempts land at 60/90 LME (66.7%) — **identical totals, different category distributions**:

| Category | v015f | v015i | v015j |
|----------|-------|-------|-------|
| knowledge-update     | 12 | 12 | 12 |
| multi-session        | 12 |  9 | **13** |
| single-session-assistant | 13 | **14** | 11 |
| single-session-preference | **13** |  8 |  9 |
| single-session-user  |  7 |  **9** |  7 |
| temporal-reasoning   |  6 |  **8** |  **8** |
| **OVERALL**          | **63** | 60 | 60 |

**Root cause**: our pipeline runs ONE retrieval profile for ALL question types. RRF fuses by source-blind rank. A rank-1 temporal noise-hit beats a rank-3 semantic gold-hit on math (`1/61 > 1/63`). The cross-encoder downstream gets a noisy pool to disambiguate.

The shuffling pattern is the signature of a **single shared candidate pool that's overloaded with mutually-contradictory signals**. Knob-tuning (the cap=5 attempt) trades one category's pollution for another's starvation. **It cannot break the structural trade-off.**

## What Hindsight does differently

Reading `hindsight-api-slim/hindsight_api/engine/memory_engine.py:3009-3211`:

1. **Per-fact-type segmented retrieval** (`for ft in fact_type:` at line 3209): all 4 channels run **inside** each fact_type loop. Each fact_type has its own fused pool. Merge happens at the end. A flood in one channel's `experience`-typed results can't displace `preference` hits because they're in different pools.

2. **Conditional channel arity** (line 3211): `if temporal_results: 4 channels else: 3 channels`. They omit temporal entirely when query has no dates.

3. **BM25 as a stable first-class channel** (not a "fifth sibling" experiment).

## What Astrocyte already has (but doesn't use)

After audit:

| Module | Lines | Wired into bench? |
|---|---|---|
| `astrocyte/pipeline/query_intent.py` | 269 | ❌ unused |
| `astrocyte/pipeline/fusion.py:weighted_rrf_fusion` | exists | ❌ unused (bench uses unweighted `_rrf_fuse_fact_hits`) |
| `astrocyte/pipeline/preset_routing.py` | 39 | ❌ unused |
| `astrocyte/pipeline/query_plan.py` | 126 | ❌ unused |
| `store.search_facts_keyword` (BM25 SPI) | exists | ❌ disabled (M31c regression notes) |

**M34 is largely a wiring + extension job, not green-field.**

## Design

### Part 1 — Intent-weighted RRF (wire existing pieces)

`fact_recall()` consults `classify_query_intent(query)` once, maps the intent to per-channel weights, and switches from `_rrf_fuse_fact_hits` to `weighted_rrf_fusion`.

**Intent → weight table** (M34's main calibration knob):

| QueryIntent (existing enum) | semantic | episodic | temporal | link_exp | bm25 | Notes |
|---|---|---|---|---|---|---|
| TEMPORAL  | 1.0 | 0.7 | **1.5** | 0.5 | 1.0 | TR-style queries; boost temporal |
| COMPARATIVE | 1.0 | 1.0 | **0.3** | 1.0 | 1.0 | "which X first" — suppress temporal flood |
| RELATIONAL | 0.8 | 1.0 | 0.5 | **1.5** | 1.0 | "how does X relate to Y" — link-expansion led |
| FACTUAL | **1.5** | 0.5 | 0.3 | 0.5 | **1.5** | "what is my X" — semantic + BM25 dominant |
| PROCEDURAL | 1.2 | 0.8 | 0.3 | 0.8 | 1.0 | "how to do X" — semantic-led |
| EXPLORATORY | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | "tell me about X" — diversity over precision |
| UNKNOWN (fallback) | 1.0 | 1.0 | **0.5** | 1.0 | 1.0 | Same as today, except temporal damped to v015j-ish |

**Critical invariant**: weight=0 mutes a channel entirely. No channel ever has negative weight (already enforced by `weighted_rrf_fusion`).

**Backward compatibility**: if intent classifier returns UNKNOWN AND existing callers pass no `intent=` arg, fall back to all-equal weights — produces identical ranking to current `_rrf_fuse_fact_hits`.

### Part 2 — Per-fact-type segmentation (the new piece)

Today: `fact_recall` runs 4 channels, fuses ONE pool, returns top-N. Fact_type is in the result metadata but ignored as a routing key.

M34: `fact_recall` accepts `fact_types: list[str] | None`. When non-None:

```
for ft in fact_types:                    # e.g. ['experience', 'preference', 'world']
    run 4-5 channels filtered to ft
    weighted_rrf_fusion → ranked_ft[ft]
merge:
    return concat(ranked_ft[ft][:per_type_k] for ft in fact_types)
```

`per_type_k = ceil(final_top_n / len(fact_types))`. Each fact_type gets a guaranteed share of the output.

**SPI changes** (minimal — most adapters already have the parameter):

- `search_facts_semantic(..., fact_type: str | None = None)`
- `search_facts_temporal(..., fact_type: str | None = None)`
- `search_facts_keyword(..., fact_type: str | None = None)`
- `search_facts_by_entity(..., fact_type: str | None = None)`

In-memory adapter: trivial — filter `if f.fact_type == ft` in the existing loop.
Postgres adapter: trivial — add `AND fact_type = %s` clause when param is non-None.

**Default fact_types for bench** (mapping to LME's 6 categories' factual basis):

```python
DEFAULT_FACT_TYPES_FOR_BENCH = ['experience', 'preference', 'world']
```

Rationale: LME's TR/MS/SSU/SSA all consume `experience` facts; SSP consumes `preference`; KU consumes `world`. Plan and opinion are minor; safe to fold into experience for the bench (no semantic distinction at retrieval time).

### Part 3 — Restore BM25 as the 5th channel (post-M33-4 reattempted, this time safe)

BM25 was disabled in M31c after the flat 5-sibling RRF regressed -6.7pp. With intent-weighted fusion:

- FACTUAL queries weight BM25 at 1.5 → SSU questions ("what's my favourite") get explicit BM25 boost
- TEMPORAL/EXPLORATORY weight BM25 at 1.0 → neutral participation
- COMPARATIVE/RELATIONAL weight BM25 at 1.0 → no harm

This sidesteps M31c's failure mode (uniform-weight 5-sibling flood) by giving BM25 *targeted* influence where it helps (SSU) and neutral elsewhere.

## Why this should break the v015i/v015j shuffling

| v015i/j failure | M34 mechanism that addresses it |
|---|---|
| SSP -5 in v015i (preference facts crowded out by experience-temporal flood) | Per-fact-type segmentation: preference facts have their own RRF pool. Plus PREFERENCE-classifier (matches "prefer/favourite") → temporal weight 0.2. |
| MS -3 in v015i (cross-session synthesis dragged by date-window noise) | RELATIONAL classifier (matches "across/throughout/ever") → link_expansion weight 1.5. |
| SSU -2 in v015j (BM25 still off, SSU loses specific-term recall) | FACTUAL classifier → BM25 weight 1.5. Per-fact-type segmentation prevents bleeds. |
| SSA -3 in v015j (cap=5 starved a channel SSA needed) | Per-fact-type segmentation keeps each fact_type's allocation independent of any single channel's cap. |
| TR +2 (the gain we want to preserve) | TEMPORAL classifier → temporal weight 1.5. Still uses the M33-3a date-population work. |

## Acceptance criteria

Hard ship-gate (un-relaxed): **LME top_50 ≥ 70.0%**.

Soft criteria:
- No LME category drops more than -1 question vs v015f
- LoCoMo top_50 ≥ 84.5% (v015f baseline)
- Latency overhead < 15% vs v015f (additional intent classifier call + per-type loops)

## Implementation plan — 6 sub-tasks

### M34-1: Map QueryIntent → channel weights
- New: `astrocyte/pipeline/intent_weights.py`
- Single dict `INTENT_CHANNEL_WEIGHTS: dict[QueryIntent, ChannelWeights]` matching the table above
- `ChannelWeights` dataclass with named fields per channel
- Helper: `weights_for_intent(intent: QueryIntent) -> ChannelWeights`
- Tests: one per intent + UNKNOWN fallback

### M34-2: fact_recall accepts intent + uses weighted_rrf_fusion
- Modify `fact_recall()` signature: `intent: QueryIntent | None = None`
- When non-None: switch from `_rrf_fuse_fact_hits([...])` to `weighted_rrf_fusion([(list, w), ...])`
- When None: preserve current unweighted behaviour (BC)
- Tests: assert ranking changes when intent → biased weights

### M34-3: Per-fact-type SPI filter
- Add `fact_type: str | None = None` param to:
  - `search_facts_semantic`
  - `search_facts_temporal`
  - `search_facts_keyword`
  - `search_facts_by_entity`
- In-memory + Postgres + provider Protocol all updated
- Tests: query with `fact_type='preference'` returns only preference facts

### M34-4: fact_recall per-fact-type segmentation
- Modify `fact_recall()` signature: `fact_types: list[str] | None = None`
- When non-None: outer loop per fact_type, inner = current 4-5 channel run with fact_type filter; per-type RRF; final merge with `per_type_k` allocation
- When None: preserve current behaviour
- Tests: segmented retrieval returns expected per-type counts

### M34-5: Wire BM25 as 5th channel in fact_recall
- Add `search_facts_keyword` call when `bm25_weight > 0` (resolved from intent)
- Pre-M34 the 5th-sibling RRF regressed; with intent weights this is bounded by INTENT_CHANNEL_WEIGHTS table (most intents have bm25 weight ≤ 1.0, only FACTUAL has 1.5)
- Tests: BM25 sibling fires only when weight > 0; backwards-compatible when intent is UNKNOWN

### M34-6: Bench wiring + execution
- `scripts/mem0_harness/astrocyte_client.py`: call `classify_query_intent(query)` once per question, pass to `fact_recall(intent=..., fact_types=DEFAULT_FACT_TYPES_FOR_BENCH)`
- Run bench v015k
- Compare against v015f hard ship-gate

## Files changed

| File | Change | LoC |
|---|---|---|
| NEW `astrocyte/pipeline/intent_weights.py` | Intent → weights mapping | ~80 |
| MOD `astrocyte/pipeline/fact_recall.py` | Accept intent + fact_types; per-type loop; weighted fusion; wire BM25 | ~100 |
| MOD `astrocyte/provider.py` | Add fact_type param to 4 SPI methods | ~20 |
| MOD `astrocyte/testing/in_memory.py` | Filter by fact_type | ~20 |
| MOD `adapters-storage-py/astrocyte-postgres/.../pageindex_store.py` | Filter by fact_type in SQL | ~30 |
| MOD `scripts/mem0_harness/astrocyte_client.py` | Call intent classifier, pass to fact_recall | ~15 |
| NEW `tests/test_intent_weights.py` | Per-intent weight map tests | ~80 |
| MOD `tests/test_fact_recall.py` | Intent-weighted + per-type segmentation tests | ~120 |
| MOD `tests/test_pageindex_store.py` | fact_type filter in SQL tests | ~60 |

Total: ~525 LoC across 9 files (most existing).

## Risk and rollback

**Risk: intent classifier misfires on bench queries** → fallback to UNKNOWN preserves current behaviour. Already enforced in `weighted_rrf_fusion`.

**Risk: per-fact-type segmentation slower** → adds `n_fact_types - 1 = 2` extra SQL queries per channel. Indexes on `(bank_id, fact_type)` already exist. <15% latency hit expected.

**Risk: BM25 5th channel re-regresses** → intent gating means it only fires with weight > 0; most queries get bm25 weight 1.0 (FACTUAL gets 1.5). Pre-M34 it was a hard 5th sibling regardless of intent — that's the failure mode M34 sidesteps.

**Rollback path**: revert `astrocyte_client.py` change (the bench wiring). The new code paths in `fact_recall` are all behind `intent is not None and fact_types is not None` guards. Without the bench wiring, behaviour is identical to v015j.

## Estimated execution

- Code: 4-6 hours focused
- Tests: 1-2 hours
- Bench wall: ~70 min
- **Total: ~1 day + 1 bench cycle**

## Out of scope (deferred to v0.16.0 / M35)

- LLM-based intent classifier (regex covers our 6 LME categories well; LLM cost not justified yet)
- Per-fact-type top_k tuning (M34 uses equal share; later cycles can shift)
- BM25 + intent A/B on a separate metric beyond M33-4-style binary comparison
