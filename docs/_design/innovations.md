# Innovations roadmap

This document describes Astrocyte's innovation roadmap — capabilities inspired by ByteRover (agent-native curation, zero-infra, progressive retrieval) and Hindsight (biomimetic memory, multi-strategy retrieval, mental models). Each innovation is independently implementable, backward-compatible, and feature-gated.

For the core architecture see `architecture.md`. For the built-in pipeline see `built-in-pipeline.md`. For **C4 / deployment / domain model** detail and **milestone sequencing** (M1–M7 for **v1.0.0** GA; **M5** + **M7** in **v0.8.0**), see `c4-deployment-domain.md` and `product-roadmap.md`.

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

## Benchmark-Exercised Hindsight Patterns

The framework now contains Hindsight-style primitives that are specifically exercised by LoCoMo benchmark config:

- Measurement: per-question result serialization, failure buckets, and a deterministic 200-question slice.
- Precision: deterministic cross-encoder-like rerank plus optional `llm_pairwise` rerank for top candidates.
- Structured retain: LoCoMo sessions persist speaker, turn, conversation, session, person, and temporal metadata.
- Mental models: per-person persona pages are retained as `wiki` facts with `_wiki_source_ids` provenance.
- Temporal facts: relative phrases are normalized at retain time and rendered beside source memories in reflect prompts.
- Multi-hop: entity-path fallback recall provides path-labeled context even when no graph backend is configured.

These are implemented in the open framework. Production-grade tuning, learned weighting, and higher-quality model selection remain execution-quality differentiators rather than gated capabilities.

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

## Phase 2: Intelligence (implemented)

### 2.1 Adaptive Tiered Retrieval (implemented)

**Inspired by:** ByteRover's 5-tier progressive escalation (0ms → 15s).
**Module:** `astrocyte/pipeline/tiered_retrieval.py`

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

**Cost tiers vs truth precedence:** The tiers above are **budget and latency** stages (try cheap recall first). They are **not** “Priority 1 graph beats Priority 2 stats beats Priority 3 vectors” *authority* tiers. Patterns that merge multiple stores into **labeled prompt sections** with **explicit precedence rules** for the LM target a different problem (structured factual precedence). Astrocyte’s **default** remains algorithmic fusion (RRF, layer weights). **Declarative authority / prompt-section recall** is roadmap **M7** (**v0.8.0**, same release as **M5** adapters), optional in config — **not** the default — and stays **separate in naming and docs** from `tiered_retrieval`; see **`product-roadmap.md` § M7** and **`built-in-pipeline.md`** §9.4.

### 2.2 LLM-Curated Retain (implemented)

**Inspired by:** ByteRover's core innovation — the reasoning LLM decides what/how to store.
**Module:** `astrocyte/pipeline/curated_retain.py`

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

### 2.3 Curated Recall (implemented)

**Originally planned for Mystique, moved to astrocyte — this is a framework capability.**
**Module:** `astrocyte/pipeline/curated_recall.py`

Post-retrieval re-scoring of recall hits by:
- **Freshness:** exponential decay on `occurred_at`
- **Source reliability:** metadata-based scoring
- **Domain salience:** similarity to bank context/mission

Re-ranks and optionally filters below quality threshold. Provider-agnostic — works with any Tier 1 or Tier 2 backend.

### 2.4 Progressive Retrieval (implemented)

**Originally planned for Mystique, moved to astrocyte — this is a protocol-level capability.**

`detail_level` field on `RecallRequest`:

| Value | Behavior | Token cost |
|---|---|---|
| `"titles"` | First sentence + metadata/score only | ~10x fewer |
| `"bodies"` | Full text (current behavior) | Normal |
| `"full"` / None | Current behavior | Normal |

Enables two-pass pattern: agent gets title manifest first, then fetches specific memories.

### 2.5 Cross-Source Fusion (implemented)

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

### 2.6 Cross-Engine Routing (implemented)

**Module:** `astrocyte/hybrid.py` — `AdaptiveRouter` class

Adaptive per-query weights in `HybridEngineProvider`. Classifies queries by:
- **Temporal signals** — date/time keywords boost engine (if it supports temporal search)
- **Entity density** — capitalized proper nouns boost engine (if it supports graph search)
- **Question complexity** — how/why/explain boost engine (if it supports reflect)
- **Query length** — short queries boost pipeline (keyword/BM25 sufficient)

```python
hybrid = HybridEngineProvider(engine=mystique, pipeline=pipeline, adaptive_routing=True)
# Temporal query → routes more weight to engine
# Simple factual → routes more weight to pipeline
```

---

## Phase 3: Declarative routing (implemented)

### 3.0 Memory Intent Protocol (MIP)

**Design doc:** `memory-intent-protocol.md`

**Implementation (Python):** `astrocyte.mip` — YAML loader, mechanical rule engine, `MipRouter` (rules first, then optional LLM intent when configured). Rust and spec-only options (e.g. integration patterns in `memory-intent-protocol.md` §6) follow the usual parallel-implementation cadence.

MIP makes memory routing declarative — both deterministic rules and LLM-based intent in one protocol. "Intent" carries two senses: the system's declared intent (rules, compliance policies, escalation paths) and the model's expressed intent (LLM judgment when rules can't resolve).

**Components:**
- **`mip.yaml`** — bank definitions, priority-ordered mechanical rules, match DSL (all/any/none, existence checks, value checks, computed signals), action DSL (bank templates, tags, retain policies, escalation)
- **Intent policy** — LLM prompt + constraints, fires only on escalation from mechanical rules
- **Override hierarchy** — compliance rules always mechanical, never delegated to model judgment
- **Retrieval/reflect triggers** — auto-recall and auto-reflect conditions

**Resolution pipeline:** Mechanical rules first → ambiguity detector → LLM intent only when rules cannot resolve. Zero inference cost for the deterministic path.

**Relationship to existing innovations:**
- LLM-curated retain (§2.2) is a subset of MIP's intent layer
- Cross-engine routing (§2.6) is orthogonal to MIP routing
- MIP does not replace these — it provides the declarative policy layer above them

---

## Phase 4: Advanced (planned)

### 4.1 Mental Model Observability (Mystique)

`ConsolidationTrace` dataclass exposing when mental models were updated, observations formed, facts consolidated, and disposition drift detected. Mystique-specific because mental models are computed by Hindsight's engine.

### 4.2 Advanced Consolidation (Mystique)

Quality-based loss functions and observation formation — Hindsight's engine advantage. Per-bank policies for when to consolidate (session-end, quota-trigger, idle-window, scheduled).

---

## Phase 5: Third-option primitives + wiki compile

These capabilities constitute the complete answer to the four diagnostic tests in *Familiarity is the Enemy* (see `platform-positioning.md` for the framing). Each is an open-source framework capability — not a Mystique exclusive.

**Shipping order:**
- **v0.8.x** — M8 (LLM wiki compile): ships before v0.9.0, eval-gated, no M9–M11 dependency
- **v0.9.0** — research track (R1–R5), parallel
- **v1.0.0** — M9 (time travel), M10 (gap analysis), M11 (entity resolution): deepen M8 when they land

The open-core split for this phase:

| Capability | Astrocyte (free) | Mystique (premium) |
|---|---|---|
| LLM wiki compile (v0.8.x) | `WikiPage` compiled memories + provenance + lint pass | Same + disposition-aware compilation + cross-bank wiki merge |
| Time travel (v1.0.0) | `retained_at` + `forgotten_at` + `as_of` filter + `brain.history()` | Same + temporal spreading activation in graph traversal |
| Gap analysis (v1.0.0) | `brain.audit()` + LLM judge + `AuditResult` | Same + multi-bank gap synthesis + scheduled gap alerts |
| Entity resolution (v1.0.0) | `EntityResolver` + `EntityLink` with evidence + `astrocyte-age` default | Same + gleaning passes (multi-pass entity extraction) + co-occurrence tracking |

### 5.1 Time Travel (M9)

**Diagnostic test:** Test 3 — "What did we believe about X on March 1st?"

**Module:** `astrocyte/pipeline/time_travel.py` (filter application); type changes in `astrocyte/types.py`

Every `VectorItem` carries two system-managed timestamps:

- `retained_at: datetime` — set on write, immutable; the moment the memory entered the bank
- `forgotten_at: datetime | None` — set on soft-delete; `None` means the memory is active

`VectorFilters` gains `as_of: datetime | None`. When set, the recall path restricts results to:

```sql
retained_at <= as_of AND (forgotten_at IS NULL OR forgotten_at > as_of)
```

A new `brain.history(bank_id, start, end)` method returns all memories retained and/or forgotten in a date range, ordered by `retained_at`.

```python
# What did the eng team know about the deployment pipeline before the March 5 incident?
hits = await brain.recall(
    "deployment pipeline",
    bank_id="eng-team",
    as_of=datetime(2026, 3, 5, tzinfo=timezone.utc),
)

# Timeline of what changed in March
timeline = await brain.history("eng-team", start=datetime(2026, 3, 1), end=datetime(2026, 3, 31))
```

**Type additions:** `retained_at: datetime` and `forgotten_at: datetime | None` on `VectorItem`; `as_of: datetime | None` on `VectorFilters`.

**Impact:** Enables post-mortem analysis, compliance audit, and accountability. The system becomes an append-only ledger of beliefs, not a mutable black box.

### 5.2 Gap Analysis (M10)

**Diagnostic test:** Test 1 — "What don't we know about X?"

**Module:** `astrocyte/pipeline/audit.py`

`brain.audit(scope: str, bank_id: str) -> AuditResult` surfaces absent topics and thin coverage areas. Unlike recall — which retrieves what exists — audit reasons about what is *missing*.

Implementation:
1. Run scoped recall against `bank_id` for `scope` topic
2. Pass recall results to a configurable LLM judge with prompt: "Given these memories about {scope}, what topics are missing, thin, or contradictory?"
3. Parse structured output into `AuditResult`

```python
result = await brain.audit("incident response procedures", bank_id="eng-team")
# AuditResult(
#   gaps=[
#     GapItem(topic="rollback procedures", severity="high",
#             reason="no memories matching rollback or revert"),
#     GapItem(topic="escalation path for database incidents", severity="medium",
#             reason="only one memory, dated 2025"),
#   ],
#   coverage_score=0.54,
#   memories_scanned=12,
# )
```

**Type additions:** `AuditResult(gaps: list[GapItem], coverage_score: float, memories_scanned: int, scope: str)`, `GapItem(topic: str, severity: Literal["high", "medium", "low"], reason: str)`.

**Impact:** Gap analysis is the primary way compliance teams verify that critical knowledge areas are adequately documented. It is also the foundation for the M8 wiki compile lint pass.

### 5.3 Entity Resolution (M11)

**Diagnostic test:** Test 2 — "Is 'Calvin' the same person as 'the CTO'?"

**Module:** `astrocyte/pipeline/entity_resolution.py`

Retain-time pipeline stage that extracts named entities, looks up graph candidates, and confirms matches with LLM-generated evidence quotes. The result is an `EntityLink` — not a cosine similarity score, but a structured assertion with provenance.

```python
# During retain of "The CTO approved the rollout":
# 1. Entity extractor: {"CTO"} detected
# 2. Graph lookup: existing entity "Calvin Cheng" is a candidate (alias lookup)
# 3. LLM confirmation: "CTO refers to Calvin Cheng (evidence: 'our CTO Calvin approved the Q4 plan')"
# 4. Write EntityLink:
EntityLink(
    type="alias_of",
    entity_a="CTO",
    entity_b="Calvin Cheng",
    evidence="our CTO Calvin approved the Q4 plan",
    confidence=0.94,
    created_at=datetime.utcnow(),
)
```

On recall, graph traversal automatically follows entity links. A query for "Calvin" retrieves memories tagged with "CTO" and vice versa.

**New `GraphStore` SPI methods:** `find_entity_candidates(name: str, threshold: float) -> list[EntityCandidate]`, `store_entity_link(link: EntityLink) -> None`.

**Default graph store — `astrocyte-age`:** Apache AGE is a PostgreSQL extension that adds a full property graph model (vertices, edges, Cypher queries) to the same instance already running pgvector. This means most deployments need zero additional infrastructure — one PostgreSQL instance provides both vector search and graph traversal.

```yaml
# astrocyte.yaml — default v1.0.0 stack
vector_store: pgvector
vector_store_config:
  connection_url: postgresql://localhost/memories  # same instance

graph_store: age
graph_store_config:
  connection_url: postgresql://localhost/memories  # same instance, no extra service

entity_resolution:
  enabled: true
  similarity_threshold: 0.75
  llm_confirmation: true
```

**Impact:** Entity resolution turns the memory graph into a structured knowledge base. Cross-document queries become semantically accurate rather than just syntactically similar. The evidence chain makes the resolution auditable and correctable.

### 5.4 LLM Wiki Compile (M8 — v0.8.x)

**All four diagnostic tests** — first step toward the full third-option system.

Full design: **[`llm-wiki-compile.md`](llm-wiki-compile.md)**. Summary:

An async `CompileEngine` maintains rewritable `WikiPage` memories (entity/topic/concept pages) from raw memories. Each `WikiPage` has:
- A synthesized summary of what the bank knows about this entity or topic
- Provenance: which raw memories contributed
- Cross-links: which other entities are mentioned
- A lint status: whether the page has contradictions or stale claims

On recall, wiki hits are ranked ahead of raw memories with fallback. A periodic lint pass catches contradictions, stale claims, and orphaned cross-links.

**Ships when**: LongMemEval compile vs no-compile A/B gate passes — ≥10pp absolute lift on `multi-session` or `knowledge-update`, no other category regressing > 2pp, retain p95 unchanged. Depends on M3 (extraction pipeline) + M5 (production storage). Ships before v0.9.0.

**Deepened by M9–M11 (v1.0.0):** When the three primitives land, M8 gains `retained_at`-stamped provenance on WikiPage edits (M9), gap-audit-driven lint detection (M10), and entity cross-links via the AGE graph (M11). These are additive enhancements — M8 ships useful without them.

---

## Backward compatibility

All innovations follow these rules:

1. **Types:** New fields have `None`/`False` defaults. Pattern: `field: type | None = None`
2. **Config:** New sections have `enabled: bool = False` as first field
3. **Pipeline:** Feature-gated with `if config.X.enabled:` — default path unchanged
4. **SPI:** No changes to Protocol method signatures. New fields are optional on request/result types.
5. **Tests:** All existing tests pass after each innovation
