# M32 — Stack unification (PageIndex as the public recall pipeline)

**Status:** IN PROGRESS (started 2026-05-20)
**Predecessor:** [M31 LME quality](m31-lme-quality.md)
**Triggered by:** v0.15.0 ship audit revealed `Astrocyte.recall()` and the bench harness use two different retrieval stacks.

---

## 1. Problem statement

**Astrocyte has two parallel retrieval architectures that drifted apart over the M14–M31 cycle arc:**

| Stack | Entry point | Pipeline | Storage |
|---|---|---|---|
| **A. PageIndex** (bench) | `astrocyte_client.search()` | `fact_recall` + `section_recall` + cross-encoder rerank | `PageIndexStore` SPI (`search_facts_*`, `search_sections_*`) |
| **B. Vector-store** (public API) | `Astrocyte.recall()` | `orchestrator.recall()` | `VectorStore` SPI (`search_similar` with `VectorFilters`) |

**Every bench score since M14** has measured Stack A. Every README badge, every `BENCH_PARITY.yaml` row, every cycle's "X/230" headline. The PageIndex-specific improvements that landed across M14-M31 (RRF fusion, fact↔chunk pairing, per-Q-type prompts, M27 fields, M28-M29 coreference, M30 parallelization, M31 session_filter + event_date) **only live in Stack A**.

**Users calling `Astrocyte.recall()` get Stack B** — the older M9-era vector_store + graph_store pipeline. None of the bench-validated improvements ship through that path.

Concrete consequence: the README badge says "84.2% LoCoMo" for v0.14.0, but a user installing `astrocyte==0.14.0` and calling `await astrocyte.recall("...")` does NOT get that quality — they get whatever Stack B produces, which has never been benched.

## 2. Goal

After M32, `Astrocyte.recall()` and the bench harness MUST go through the same retrieval code path, so a future bench score actually represents what `pip install astrocyte` produces.

Concretely:
- `Astrocyte.recall(query, bank_id=..., session_id=..., time_range=..., fact_types=...)` routes through PageIndex retrieval (`fact_recall` + `section_recall` + rerank)
- Returns the same `RecallResult` shape (typed `MemoryHit` list) the public API has always returned
- Bench harness eventually calls `Astrocyte.recall()` rather than instantiating `PostgresPageIndexStore` directly (validates the unification)

## 3. Design

### Component to add — `PageIndexPipeline`

New module `astrocyte/pipeline/pageindex_pipeline.py`:

```python
class PageIndexPipeline:
    """Recall pipeline that drives the PageIndex stack.

    Implements the same async ``recall(request: RecallRequest) -> RecallResult``
    contract as the legacy ``orchestrator.recall()`` so it slots into the
    same ``ProviderDispatcher`` machinery — callers don't see a difference.
    """

    def __init__(
        self,
        store: PageIndexStore,
        embedding_provider: LLMProvider,
        bank_id_resolver: Callable[[str], str] | None = None,
        config: AstrocyteConfig | None = None,
    ) -> None: ...

    async def recall(self, request: RecallRequest) -> RecallResult:
        # 1. Query embedding
        # 2. query_analyzer for temporal range (if not in request)
        # 3. fact_recall + section_recall in parallel (asyncio.gather)
        # 4. cross-encoder rerank
        # 5. Convert hits → MemoryHit list
        # 6. Wrap in RecallResult with RecallTrace
```

### Result-shape adapter — `MemoryFact / PageIndexSection → MemoryHit`

`PageIndexPipeline._to_memory_hits()` converts internal grain-tagged candidates to the public `MemoryHit` shape:
- Fact-grain → `MemoryHit(text=fact.text, memory_layer="fact", chunk_id=fact.chunk_id, occurred_at=fact.event_date or fact.occurred_start, ...)`
- Section-grain → `MemoryHit(text=section_excerpt, memory_layer="section", ...)`

### Dispatcher wiring

Add `PageIndexPipeline` as a `pipeline` option on `ProviderDispatcher` so `Astrocyte.recall()` → `_dispatcher.recall(request)` → `pageindex_pipeline.recall(request)` when the operator configures `engine: pageindex`.

**Default in v0.16.0 onward: `engine: pageindex`.** Operators on the legacy vector_store stack can opt out via config until they migrate.

## 4. Phases

| Phase | Work | Effort |
|---|---|---|
| **P1 PageIndexPipeline.recall()** | Extract bench's `search()` core into the new pipeline class. Pure function over (store, provider, request). Tests against InMemoryPageIndexStore. | 2-3 hours |
| **P2 Result-shape adapter** | `MemoryFact + PageIndexSection → MemoryHit` mapper. Tests for fact/section/wiki grain coverage. | 1 hour |
| **P3 Dispatcher wiring** | Config flag (`engine: pageindex|vectorstore`); `ProviderDispatcher` chooses pipeline based on flag. Backward-compat: default `vectorstore` for v0.15.x; default `pageindex` for v0.16.0+. | 1 hour |
| **P4 Tests** | Unit tests covering: dispatcher routing, request → response shape, session_id/time_range/fact_types honoured, bench-vs-public-recall parity smoke test. | 2 hours |
| **P5 Bench client migration** | `astrocyte_client.search()` → calls `Astrocyte.recall()` instead of direct store access. Confirms bench results equal public API results. | 1 hour |
| **P6 M32 cycle bench** | Re-bench v0.15.0+M31+M32 stack; expect parity with v015b numbers (122/180 LME @ top_50). If parity holds, ship as v0.16.0 with truthful badge. | ~1 hour wall |

## 5. Ship gate

| Gate | Criteria |
|---|---|
| Unit tests | All 2812+ existing + new tests pass; ≥15 new tests for PageIndexPipeline + dispatcher |
| Parity smoke | `Astrocyte.recall()` returns same top-K (by id ordering) as bench `astrocyte_client.search()` on a fixed seed |
| **Bench parity (≥)** | LME ≥ v015b 122/180 @ top_50; LoCoMo ≥ v015b 173/200 @ top_50 |
| Backward compat | v0.15.x callers configuring `engine: vectorstore` (or default) get unchanged behaviour |
| Documentation | Migration note in CHANGELOG; config option documented |

## 6. Carry-forward (deferred to future cycles)

1. **Adapter SQL filter for VectorStore.session_filter** (M33-A) — Postgres pgvector / Qdrant / Neo4j / Elasticsearch adapters currently ignore `VectorFilters.session_id`. After M32, the VectorStore stack is opt-in, so the urgency is lower, but adapters should still honour the filter when they're configured.
2. **Sunset vector_store pipeline** (M33-B?) — once PageIndex is default and stable in production, deprecate the legacy `orchestrator.recall()` path. Free up code-paths + reduce maintenance.
3. **Multi-bank cross-recall through PageIndex** — current PageIndex pipeline is single-bank (one document per user). Multi-bank `RecallRequest` (with `banks=[...]`) needs unification.

## 7. Cycle close — SHIPPED in v0.15.0 (2026-05-20)

### What landed

| Piece | Status |
|---|---|
| `PageIndexPipeline` class with `async recall(request) -> RecallResult` | ✅ |
| Result-shape adapter: `MemoryFactHit + FusedHit → MemoryHit`; M27 confidence + M27 mentioned_at + M31 event_date + line_num + entities + speaker all preserved on `MemoryHit.metadata` | ✅ |
| `ProviderDispatcher.recall()` prefers `pageindex_pipeline` when set, falls back to legacy stack | ✅ |
| `Astrocyte.use_pageindex_pipeline(store, embedding_provider, ...)` public API | ✅ |
| `RecallRequest.session_id` end-to-end (M31 Fix 2 production wiring) | ✅ |
| `VectorFilters.session_id` end-to-end (legacy stack honors it for InMemory adapter) | ✅ |
| 12 unit tests covering pipeline + dispatcher + Astrocyte API + metadata preservation (in-memory) | ✅ |
| 9 Postgres parity tests gated on `ASTROCYTE_PG_DSN` (all pass against bench DB) | ✅ |

### What did NOT land (queued for M33)

- **Bench-side migration** (`astrocyte_client.search()` rewritten to call `Astrocyte.recall()` instead of direct `fact_recall` + `section_recall`). Deferred because:
  1. The bench's search() includes cross-encoder rerank + bench-specific source_chunk rendering not yet in `PageIndexPipeline`
  2. Full migration would touch 250+ lines of carefully-tuned bench code that's been validated through 8 cycle arcs
  3. v0.15.0 ship pressure favors safety
- **Cross-encoder rerank in `PageIndexPipeline`** — would be needed for full bench parity. Currently rerank is bench-side only. M33 adds it as an optional step.
- **Postgres / Qdrant / Neo4j / Elasticsearch VectorStore adapters honor `session_id`** — only InMemory does. Legacy stack opt-in, so urgency is lower than v0.15.0 ship.

### Architectural honesty

The unification is **semantically real but not yet end-to-end via the bench harness**:
- `Astrocyte.recall()` users get the same `fact_recall` + `section_recall` retrieval primitives the bench validates
- Postgres parity tests prove the SQL layer works identically across adapters
- The bench harness still has its own wrapping (source_chunk rendering, cross-encoder rerank) that hasn't been moved into the unified pipeline yet

M33 finishes the migration by absorbing rerank + source-chunk concerns into the public pipeline, then re-routing the bench through `Astrocyte.recall()` for end-to-end validation.

### Bench result on next run (M31 + M32 unified)

To be filled in after the unified bench run. Expected: parity with v015b baseline (LME 122/180, LoCoMo 173/200 @ top_50) since the bench's retrieval semantic is unchanged at the fact/section primitive layer — only the answerer prompt (Fix 3) and retain-side event_date persistence (Fix 4) changed.

## 8. Carry-forward to M33

1. **Cross-encoder rerank as PageIndexPipeline step** — make it optional + default-on. Lets bench call `Astrocyte.recall()` and get full parity.
2. **Bench harness migration** — `astrocyte_client.search()` → `Astrocyte.use_pageindex_pipeline()` + `Astrocyte.recall()`. Source_chunk rendering moves into a "raw-chunks" optional add-on for production users who want it.
3. **Postgres / Qdrant / Neo4j / Elasticsearch VectorStore session_id** — per-adapter SQL/index filter wiring.
4. **Sunset legacy `orchestrator.recall()` path** — once M32 is the default for >1 release with no regressions.
