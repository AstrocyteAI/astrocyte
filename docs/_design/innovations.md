# Innovations roadmap

This document describes Astrocyte's innovation roadmap — capabilities inspired by ByteRover (agent-native curation, zero-infra, progressive retrieval) and Hindsight (biomimetic memory, multi-strategy retrieval, mental models). Each innovation is independently implementable, backward-compatible, and feature-gated.

For the core architecture see `architecture-framework.md`. For the built-in pipeline see `built-in-pipeline.md`.

---

## Design principle: open-core split

**Astrocyte (open source)** owns **what, when, and the policy** — what to store, when to retrieve, how to govern, how to orchestrate.

**Mystique (proprietary)** owns **how well** — better retrieval algorithms, deeper synthesis, smarter consolidation. Same operations, better results.

Users on the free tier get **every capability**. Users on Mystique get **the same capabilities, executed better**. No capability is withheld from the open-source framework — Mystique's advantage is in execution quality, not feature gating.

| Capability | Astrocyte (free) | Mystique (premium) |
|---|---|---|
| Recall cache | Framework-level LRU cache | Same (provider-agnostic) |
| Memory hierarchy | Layer-weighted RRF fusion | Same + mental model formation |
| Utility scoring | Recency/frequency/relevance composite | Same + quality-based consolidation |
| Tiered retrieval | 5-tier progressive escalation | Same (provider-agnostic) |
| LLM-curated retain | ADD/UPDATE/MERGE/SKIP/DELETE curation | Same + deeper entity resolution |
| Curated recall | Freshness/reliability/salience re-scoring | Same (post-retrieval, provider-agnostic) |
| Progressive retrieval | `detail_level: "titles"` for token savings | Same (protocol-level) |
| Cross-source fusion | `external_context` for RAG/graph blending | Same + Hindsight-specific optimization |
| Cross-engine routing | Adaptive per-query weights | N/A (framework-level) |
| Reflect | Single-pass LLM synthesis | Agentic multi-turn with tool use + dispositions |
| Consolidation | Basic dedup + archive | Quality-based loss functions + observation formation |
| Entity resolution | Basic NER + exact dedup | Canonical resolution + co-occurrence + spreading activation |
| Retrieval fusion | Standard RRF | Tuned RRF + cross-encoder reranking |
| Scale | Single process | Multi-tenant, distributed |

---

## Phase 1: Foundation (implemented)

### 1.1 Recall Cache

**Inspired by:** ByteRover's Tier 0/1 cache — most queries resolve from cache without hitting storage.

**Module:** `astrocyte/pipeline/recall_cache.py`

LRU cache keyed by query embedding cosine similarity. On retain, the affected bank's cache is invalidated (contents changed). Configured via `HomeostasisConfig.recall_cache`.

```python
cache = RecallCache(similarity_threshold=0.95, max_entries=256, ttl_seconds=300)

# Before retrieval: check cache
cached = cache.get(bank_id, query_vector)
if cached:
    return cached  # Skip embedding + retrieval + fusion

# After retrieval: store result
cache.put(bank_id, query_vector, result)

# On retain: invalidate
cache.invalidate_bank(bank_id)
```

**Impact:** 5-10x latency reduction for repeated/similar queries. 80% of steady-state workloads resolve from cache.

### 1.2 Memory Hierarchy

**Inspired by:** Hindsight's Facts → Mental Models + ByteRover's Context Tree hierarchy.

Three-layer memory model with weighted recall scoring:

| Layer | What it contains | Default weight |
|---|---|---|
| `fact` | Raw retained content | 1.0 |
| `observation` | Patterns noticed across facts | 1.5 |
| `model` | Consolidated understanding | 2.0 |

**Type additions:**
- `memory_layer: str | None` on `VectorItem`, `VectorHit`, `MemoryHit`, `ScoredItem`
- `layer_weights: dict[str, float] | None` on `RecallRequest`
- `layer_distribution: dict[str, int] | None` on `RecallTrace`

**Fusion:** `layer_weighted_rrf_fusion()` applies multiplicative weights per layer after standard RRF, then re-sorts.

```python
hits = await brain.recall(
    "What does Calvin prefer?",
    bank_id="user-123",
    layer_weights={"fact": 1.0, "observation": 1.5, "model": 2.0},
)
# Models ranked 2x above raw facts
```

### 1.3 Utility Scoring

**Inspired by:** ByteRover's lifecycle metadata (importance, maturity, decay) + Hindsight's consolidation quality.

**Module:** `astrocyte/pipeline/utility.py`

Per-memory composite score:

```
utility = recency × 0.3 + frequency × 0.2 + relevance × 0.3 + freshness × 0.2
```

| Component | What it measures | Decay |
|---|---|---|
| `recency` | Time since last recall | Exponential (half-life 7 days) |
| `frequency` | How often recalled (normalized to 0-1) | None (counter) |
| `relevance` | Average relevance score when recalled | None (running average) |
| `freshness` | How new the memory is | Exponential (half-life 28 days) |

**Type addition:** `utility_score: float | None` on `MemoryHit`.

`UtilityTracker` maintains per-memory stats in memory with LRU eviction (max 10K entries).

---

## Phase 2: Intelligence (planned)

### 2.1 Adaptive Tiered Retrieval

**Inspired by:** ByteRover's 5-tier progressive escalation (0ms → 15s).

Progressive recall escalation — cheaper tiers tried first, escalate only when needed:

| Tier | Strategy | Latency | Cost |
|---|---|---|---|
| 0 | Recall cache hit | ~0ms | Free |
| 1 | Fuzzy text match on recent memories | ~5ms | Free |
| 2 | BM25 keyword search only | ~50ms | Free |
| 3 | Full multi-strategy (semantic+graph+BM25+temporal) | ~200ms | Embedding API |
| 4 | Agentic recall (LLM reformulates query + retry) | ~3-10s | LLM API |

Escalation stops when `min_results` (default 3) with `min_score` (default 0.5) is satisfied. `max_tier` (default 3) caps escalation.

**Type addition:** `tier_used: int | None` on `RecallTrace`.

### 2.2 LLM-Curated Retain

**Inspired by:** ByteRover's core innovation — the reasoning LLM decides what/how to store.

Opt-in curation mode where the LLM analyzes incoming content against existing memories and decides:

| Action | When | What happens |
|---|---|---|
| `ADD` | Genuinely new information | Store as new memory |
| `UPDATE` | Existing memory is outdated | Replace with new version, keep provenance |
| `MERGE` | Multiple memories about same topic | Consolidate into one richer memory |
| `SKIP` | Redundant or low-value | Don't store (better than post-hoc dedup) |
| `DELETE` | New info contradicts old | Remove outdated memory |

Also assigns `memory_layer` (fact/observation/model) during curation.

**Type additions:** `retention_action`, `curated`, `memory_layer` on `RetainResult`.

### 2.3 Curated Recall (re-scoring)

**Originally planned for Mystique, moved to astrocyte — this is a framework capability.**

Post-retrieval re-scoring of recall hits by:
- **Freshness:** exponential decay on `occurred_at`
- **Source reliability:** metadata-based scoring
- **Domain salience:** similarity to bank context/mission

Re-ranks and optionally filters below quality threshold. Provider-agnostic — works with any Tier 1 or Tier 2 backend.

### 2.4 Progressive Retrieval

**Originally planned for Mystique, moved to astrocyte — this is a protocol-level capability.**

`detail_level` field on `RecallRequest`:

| Value | Behavior | Token cost |
|---|---|---|
| `"titles"` | First sentence + metadata/score only | ~10x fewer |
| `"bodies"` | Full text (current behavior) | Normal |
| `"full"` / None | Current behavior | Normal |

Enables two-pass pattern: agent gets title manifest first, then fetches specific memories.

### 2.5 Cross-Source Fusion

**Originally planned for Mystique, moved to astrocyte — this is an orchestration capability.**

`external_context` field on `RecallRequest`: callers pass in results from external RAG or graph systems, which the framework fuses with provider recall results under one token budget.

```python
# Caller fetches RAG results separately
rag_hits = await rag_client.search("deployment pipeline")

# Fuse with memory recall
hits = await brain.recall(
    "How does deployment work?",
    bank_id="team",
    external_context=[MemoryHit(text=r.text, score=r.score) for r in rag_hits],
)
# → Fused results from memory + RAG, deduplicated, budget-enforced
```

### 2.6 Cross-Engine Routing

Adaptive per-query weights in `HybridEngineProvider`. Classifies queries (temporal, entity-rich, analytical, simple) and routes to the best engine accordingly.

---

## Phase 3: Advanced (planned)

### 3.1 Mental Model Observability (Mystique)

`ConsolidationTrace` dataclass exposing when mental models were updated, observations formed, facts consolidated, and disposition drift detected. Mystique-specific because mental models are computed by Hindsight's engine.

### 3.2 Advanced Consolidation (Mystique)

Quality-based loss functions and observation formation — Hindsight's engine advantage. Per-bank policies for when to consolidate (session-end, quota-trigger, idle-window, scheduled).

---

## Backward compatibility

All innovations follow these rules:

1. **Types:** New fields have `None`/`False` defaults. Pattern: `field: type | None = None`
2. **Config:** New sections have `enabled: bool = False` as first field
3. **Pipeline:** Feature-gated with `if config.X.enabled:` — default path unchanged
4. **SPI:** No changes to Protocol method signatures. New fields are optional on request/result types.
5. **Tests:** All existing tests pass after each innovation
