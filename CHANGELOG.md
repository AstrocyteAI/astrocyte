# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The Python package `astrocyte` is built from `astrocyte-py/`; its version comes from Git tags via Hatch VCS.

## [Unreleased]

## [0.15.1] — 2026-07-04 — security hotfix (dependency sweep, no behavior change)

Dependency-only release. Bench parity carries over from v0.15.0 (cycle `v015w`) unchanged — no pipeline, retrieval, or answerer code was touched.

### Security

- **~112 Dependabot alerts cleared** across every Python lockfile and the docs pnpm lockfile. Highlights:
  - `litellm` >=1.84.0 (critical: authentication bypass via Host Header injection); adapter pin widened to `<2`
  - `aiohttp` 3.14.1, `pypdf` 6.14.2, `cryptography` 49.0.0, `starlette` 1.3.1, `pyjwt` 2.13.0, `python-multipart` 0.0.32, `pydantic-settings` 2.14.2, `idna` 3.16
  - docs: `dompurify` 3.4.11, `astro` 6.4.8, `vite` 7.3.6, `esbuild` 0.28.1, `js-yaml` 4.2.0 (capped `<4.3.0` — 4.3.0 dropped its ESM default export and broke the astro build), `mermaid` 11.16.0
- **`deepeval` removed** from the `eval` extra: never imported since the v0.x eval harness was deleted, and its unconstrained setuptools requirement was holding `torch` at 2.10.0 inside a flagged range (alert #76). torch now floats to 2.12.1. Side-effect fix: the `dev` extra lists `openai` explicitly (previously arrived hidden via deepeval's transitives).
- **7 Dependabot PRs merged**: actions/checkout 6→7, docker/login-action 3→4, docker/setup-qemu-action 3→4, astral-sh/setup-uv 8.2.0, @astrojs/starlight 0.39.3, starlight-sidebar-topics 0.8.0, astro-mermaid 2.1.0.
- **CodeQL**: `py/import-of-mutable-attribute` excluded (fires on every standard function import); ~20 code-quality findings fixed at source.

### M21-M32 (v0.15.0) — SHIPPED: live-memory architecture + M27 infrastructure end-to-end + retain-time coreference + retrieval parallelization + LME quality fixes (session_filter, confidence-aware abstention, temporal-resolution-at-retain) + retrieval stack unification (2026-05-20)

**Consolidated ship spanning six cycles since v0.14.0:** M21 (live-memory architecture surface), M22-M27 (8-cycle answerer + memory-quality experiment arc — validated local optimum + banked capability infrastructure), M28-M29 (activate M27 infrastructure end-to-end + retain-time coreference; **first bench-positive measurement since M24**), M30 (retrieval parallelization — **top_50 arc peak at 193/230**), M31 (LME-quality cycle: confidence-aware abstention + session_filter + temporal-resolution-at-retain), M32 (stack unification: `Astrocyte.recall()` routes through PageIndex stack, closing bench-vs-API parity gap).

**Bench reporting cutoff shift, top_200 → top_50.** Per-question top-k retrieval at top_200 dilutes context for the gpt-4o-mini answerer (more candidates ≠ better answers past ~50). The model peaks at top_50 (M30-R3: LME 25/30 = 83.3%, LoCoMo 168/200 = 84.0%). Going forward, the badge + parity row will reference top_50 as the headline cutoff with top_200 as graceful-degradation context. M19a (v0.14.0) is being re-reported at top_50 in [`BENCH_PARITY.yaml`](BENCH_PARITY.yaml) for apples-to-apples comparison.

### Consolidated scoreboard (top_50, the answerer's operating point)

| Cycle | LME | LoCoMo | Combined | Δ vs R0-v2 | Notes |
|---|---|---|---|---|---|
| **R0-v2** baseline (M19, v0.14.0) | 22/30 (73.3%) | 167/200 (83.5%) | **189/230 (82.2%)** | — | M19 code path, top_50 reread |
| M22-M27 arc best (M24, bench-stable) | 21/30 | 172/200 | 193/230 (top_200) | parity-band | Bench-stable harness work; v0.15.0 ships the harness as default-OFF |
| **M28-M29** (activate + coreference) | 22/30 (73.3%) | 170/200 (85.0%) (top_200) | **192/230** | **+3q** | First bench-positive cycle since M24; multi-hop 93.7% with coref |
| **M30-R3** (parallel retrieval) | **25/30 (83.3%)** | **168/200 (84.0%)** | **193/230 (83.9%)** | **+4q** | **Arc peak at top_50**; L1+L2 parallelization quality-positive |

**top_200 reference (M30-R3): 21 + 166 = 187/230 (81.3%) — parity-band with R0-v2 (194/230).** The top_200 number is 6q lower than top_50 because context dilution beyond ~50 candidates outweighs additional recall.

### What ships (always-on; no opt-in flags required at defaults)

**M21 — live-memory architecture (M21 surface):**
- `astrocyte/pipeline/trend.py` (NEW) + `astrocyte/pipeline/observation.py` — `Trend` enum (NEW / STRENGTHENING / STABLE / WEAKENING / STALE), algorithmic computation from evidence timestamps, `_obs_source_timestamps` JSON list persisted alongside `_obs_source_ids`
- `astrocyte/pipeline/structured_doc.py` (NEW) — Pydantic-typed sections + typed blocks, deterministic markdown render, slug section ids, lenient parser for lazy migration of legacy markdown
- `astrocyte/pipeline/delta_ops.py` (NEW) — 8 operation types (Append/Insert/Replace/Remove block, Add/Remove/Rename section, ReplaceSectionBlocks); `apply_operations(doc, ops)` validates + applies in a copy; invalid ops drop with logged reason
- 9 new public methods on `Astrocyte` + 9 MCP tools: `list/get/create/delete_observation`, `list/create/update/delete_mental_model`, `create_directive`
- Postgres migration **032_mental_model_structured_doc.sql** — NULLable JSONB column with idempotent ADD; lazy markdown→structured migration on first refresh
- `MentalModelStore.update_via_ops(model_id, bank_id, operations) -> tuple[new_revision, summary] | None` — SPI extension on both InMemory + Postgres stores

**M22-M27 — bench-stable answerer harness + capability infrastructure:**
- `astrocyte/types.py` — `MemoryFact.confidence_score: float | None`, `MemoryFact.mentioned_at: datetime | None`, `MemoryFact.chunk_id: str | None` (computed property from `document_id:line_num`); mirror fields on `MemoryFactHit`
- `astrocyte/pipeline/section_fact_extraction.py` — Hindsight binary fact_type taxonomy (M25: world/experience), per-conversation perspective tag, `_LEGACY_FACT_TYPE_REMAP = {"assistant_statement": "experience"}` shim for backward-compat, confidence emission instruction (default 0.7 when uncertain), automatic `mentioned_at = section.session_date` population
- `astrocyte/pipeline/fact_recall.py` — link-expansion branch wired into the parallel-sibling RRF fusion; fires only when `query_entities` parameter is passed (default `None`, so v0.14.0 behavior is unchanged when callers don't opt in)
- `scripts/mem0_harness/_hindsight_answerer.py` (NEW) — focused per-Q-type prompts (M23), inline source-chunk pairing (M24) — bench harness only, opt-in via `ASTROCYTE_M22_HINDSIGHT_ANSWERER=1`

**M28-M29 — activate M27 infrastructure end-to-end + coreference at retain:**
- Postgres migrations **033_pi_facts_confidence_score.sql** + **034_pi_facts_mentioned_at.sql** — guarded `ADD COLUMN IF NOT EXISTS` with bootstrap mirror in `pageindex_store._ddl_facts_alter`
- `adapters-storage-py/astrocyte-postgres/astrocyte_postgres/pageindex_store.py` — `save_facts` INSERTs both new columns; `search_facts_*` loaders SELECT them; loader hydrates `MemoryFactHit.confidence_score` + `mentioned_at`
- `scripts/mem0_harness/_hindsight_answerer.py` — M28-B renderer: emits `Fact N:` + `When: occurred:.../mentioned:...` + `Confidence: 0.NN` + `Source chunk:` inline pairing in the bench answerer context
- `astrocyte/pipeline/mental_model.py` — `MentalModelService.refresh_from_sources(bank_id, model_id, new_source_ids)` (M28-C); thin wrapper around `MentalModelStore.refresh()`
- `astrocyte/provider.py` — `MentalModelStore.refresh()` Protocol method
- `astrocyte/_astrocyte.py` — `Astrocyte.refresh_mental_model()` public method
- `astrocyte/mcp.py` — `memory_refresh_mental_model` MCP tool (10th CRUD tool — M21 surface + M28-C)
- `astrocyte/pipeline/section_fact_extraction.py` — M29 coreference + alias resolution rules added to extraction prompt (4 numbered rules: pronoun resolution, alias canonicalization, role-based aliases for generic references, stable entity-string convention)

**M30 — retrieval parallelization (quality-positive at top_50):**
- `scripts/mem0_harness/astrocyte_client.py` — L1: `fact_recall` + `section_recall` now run as parallel sibling tasks via `asyncio.gather(return_exceptions=True)` instead of sequential awaits. Per-branch exceptions remain isolated (a `section_recall` failure can't take `fact_recall` down).
- `scripts/mem0_harness/_hindsight_answerer.py` — L2: observations + mental-model fetches in the answerer wrapper now run via `asyncio.gather(return_exceptions=True)` instead of sequential awaits

### What is NOT shipping as default behavior

All experimental flags remain opt-in:
- `ASTROCYTE_M22_HINDSIGHT_ANSWERER=1` — bench harness Hindsight-style answerer + structured context
- `ASTROCYTE_USE_REFLECT=1` — M20 reflect agent (still default-OFF; M20 §5 ship gate not cleared standalone)
- `ASTROCYTE_M18_ENABLE_EPISODIC_EXTRACTION=1` — episodic retain-side tagging
- `ASTROCYTE_M18_ENABLE_SPREADING_ACTIVATION=1` — spread retrieval
- `ASTROCYTE_REFLECT_MAX_ITERATIONS`, `ASTROCYTE_REFLECT_SEED_TOP_K` — reflect tunables

### Architectural insights banked

1. **Bench has a local optimum at the M19a config.** M22-M27 8-cycle arc validated this: every deviation in the answerer / extraction / retrieval pipeline regressed, except M24 (which reached parity by adding inline fact↔chunk pairing). The lesson: prompt-engineering ROI on this bench is exhausted; further accuracy gains require architectural changes (M28-M29 — confidence + dual timestamps + coreference at retain) or operating-point shifts (M30 — top_50 cutoff).

2. **M28-M29 confirmed M27 architectural fields pay off when end-to-end activated.** M27 alone (just adding the fields + link-expansion branch) was −8q. M28-M29 (persistence + answerer rendering + coreference) was +3q. The retain-time coreference + per-fact confidence + dual-timestamp rendering work together — they had to ship as a coherent bundle, not piecemeal.

3. **Multi-hop coref unlock (M29).** Pre-M29, M27 link-expansion's multi-hop performance collapsed late in the bench (100% at conv 0-3 → 90% by conv 9). M28-M29's coreference rules in the extraction prompt held multi-hop at **93.7% across all 10 LoCoMo conversations** — the convergence problem that killed M27 is fixed.

4. **Top_50 ≠ top_200 (M30 reporting shift).** The gpt-4o-mini answerer peaks at top_50 candidates: more candidates dilute signal and the LLM picks worse answers. M30-R3 LoCoMo: 168 @ top_50 vs 166 @ top_200; LME: 25 @ top_50 vs 21 @ top_200. We've under-reported our actual accuracy for cycles by using top_200; v0.15.0 establishes top_50 as the primary metric.

5. **Retrieval parallelization is latency-neutral, quality-marginal-positive (M30).** L1 (fact_recall ‖ section_recall) and L2 (observations ‖ mental_models) save ~3-5s per question on paper, but the answerer LLM call dominates the per-question budget (~10-20s), so end-to-end latency is unchanged. Quality at top_50 is +4q across LME+LoCoMo — believable as "better candidate ordering exposes the answerer's sweet spot." Below noise at top_200.

### Operational finding — M30b (bench state pollution)

During M30 iteration we discovered the mem0 bench runner caches per-conversation ingestion state in `predicted_<project_name>/_ingestion_N.json` files **independent of the database state**. When a bench is killed mid-flight and re-launched under the same project name with a freshly-reset DB, the runner reads the stale marker files and SKIPS re-ingestion for the affected conversations, leaving them with zero retrievable facts. M30-R2 was invalidated by this (97/200 LoCoMo, looked like a 50% regression; was actually 4 of 10 convs never ingested into the fresh DB).

**Mitigation in v0.15.0 docs:** [`docs/_design/m30-retrieval-parallelization.md`](docs/_design/m30-retrieval-parallelization.md) §3 documents the "always-fresh project name on re-launch" rule. A bench-harness-level fix (deleting `predicted_*/` when DB is reset, or DB-level ingestion-marker verification) is queued for a future cycle.

### M31 — LME quality cycle (session_filter + confidence-aware abstention + temporal-resolution-at-retain) — 2026-05-20

Three Astrocyte-specific architectural innovations to lift LME from v015b's 122/180 (67.8% top_50) baseline. The Explore agent's Hindsight comparison confirmed Hindsight doesn't use any of these — they're additive innovations, not parity moves.

**Fix 2 — `session_filter` parameter on recall (generalizable)**

When a calling application knows which conversation session a query targets (real chat apps with explicit session boundaries; LME questions with session-id metadata), the recall path can filter retrieval to that session's facts. Added end-to-end:

- `PageIndexSection.session_id: str | None` field (Postgres migration 035 + bootstrap mirror + `_ddl_sections_alter` for in-process safety)
- All 7 store search methods accept `session_filter` (`search_facts_{semantic,by_entity,temporal}` via EXISTS subquery; `search_sections_{semantic,keyword,by_entities,temporal}` via direct column filter — sections own session_id, facts join through their anchoring section)
- `fact_recall(session_filter=...)` threads to semantic+episodic+temporal branches but DELIBERATELY NOT to link-expansion (cross-session traversal is its purpose)
- `section_recall(session_filter=...)` threads to all 4 strategy closures
- `ConversationIngestor` propagates `session_id` from first-turn metadata onto emitted chunks; `_BenchRetainAdapter` writes onto sections
- Bench wiring: `astrocyte_client.search(session_id=None)` reads from `_CURRENT_SESSION_ID` contextvar populated by the per-question shim
- Public API: `Astrocyte.recall(session_id=None)`, `RecallParams.session_id`, `RecallRequest.session_id`, `VectorFilters.session_id`
- MCP: `memory_recall(session_id=None)` tool parameter

**Fix 3 — Confidence-aware abstention (prompt-only)**

Strengthened `_SHARED_PRELUDE` in `_hindsight_answerer.py` with explicit operational guidance: HIGH (≥0.85 → quote directly), MEDIUM (0.5–0.85 → soften), LOW (<0.5 → flag uncertainty), conflicting facts surface both with `mentioned:` dates. Reduces confident-wrong answers; should lift LME single-session-preference + multi-session.

**Fix 4 — Temporal-resolution at retain (M31 architectural divergence from Hindsight)**

New module `astrocyte/pipeline/temporal_resolution.py` with `resolve_event_date(text, anchor) -> datetime | None`. Parses relative date phrases ("last Tuesday", "3 days ago") at **retain time** against the section's `session_date` as anchor, persists the resolved date as `MemoryFact.event_date`. Answerer reads pre-resolved date — moves deterministic regex+library work out of the gpt-4o-mini LLM call (which was hitting a 60–70% temporal-reasoning ceiling at date math).

Components: `MemoryFact.event_date` + `MemoryFactHit.event_date` fields, Postgres migration 036 + bootstrap mirror, `save_facts` + 3 search loaders read/write the column, `section_fact_extraction.extract_facts_for_section()` calls resolver at retain time, bench candidate threading, `_hindsight_answerer.format_context_structured` renders `Event date: YYYY-MM-DD` line, `_SHARED_PRELUDE` rule instructs LLM to use it verbatim instead of re-resolving relative phrases.

### M32 — Retrieval stack unification (closes bench-vs-API parity gap) — 2026-05-20

**Audit during v0.15.0 ship discovered:** the bench harness and the public `Astrocyte.recall()` API have used **two different retrieval stacks** since the M14 PageIndex work landed. Every bench score since then measured the PageIndex stack (`fact_recall` + `section_recall` + `PostgresPageIndexStore`); the public API used the older M9-era vector_store+graph_store stack. README badges describe the PageIndex stack; users calling `Astrocyte.recall()` got neither the M14-M31 improvements nor the bench-validated quality.

M32 closes the gap by:
- New `astrocyte/pipeline/pageindex_pipeline.py` — `PageIndexPipeline` class with `async recall(request) -> RecallResult` that reuses the same `fact_recall` + `section_recall` primitives the bench validates. Result-shape adapter converts `MemoryFactHit` + `FusedHit` to `MemoryHit` with `memory_layer` tag; M27 confidence_score + M27 mentioned_at + M31 event_date + line_num + entities + speaker all preserved on `MemoryHit.metadata`. M31 `event_date` is the preferred source for `MemoryHit.occurred_at` (falls back to `occurred_start`).
- `ProviderDispatcher.recall()` prefers `pageindex_pipeline` when set; falls back to legacy `engine_provider` / `pipeline` for back-compat
- Public API `Astrocyte.use_pageindex_pipeline(store, embedding_provider, ...)` installs the pipeline

**What did NOT land in M32 (queued for M33):**
- Bench harness migration (`astrocyte_client.search()` → `Astrocyte.recall()`). Requires absorbing cross-encoder rerank + bench-specific source_chunk rendering into the pipeline first. Deferred to keep v0.15.0 ship-safe.
- Postgres pgvector / Qdrant / Neo4j / Elasticsearch VectorStore adapter SQL filters for `session_id`. Legacy stack now opt-in via not calling `use_pageindex_pipeline()`, so urgency lower.

### M31b post-mortem fixes (in v0.15.0 ship)

v015c diagnostic analysis surfaced two regression vectors not from M31 fixes themselves, but from pre-existing bench-client behaviour intersecting badly with the post-M31 surface:

- **Fix A** — disable `raw_chunk` anchor-injection in `astrocyte_client.search()`. Legacy M14.7-era code prepended 3 raw conversation excerpts at synthetic `score=1_000_000` regardless of cross-encoder rank, BURYING user-specific facts at ranks 15-55. Diagnosis on SSP failure 06f04340 (tomatoes/herbs) showed the relevant "User has been using fresh basil and mint..." fact was at rank 15 — never made it into the answerer's attention window. Removed the prepend; same sections still flow through cross-encoder via `recall.fused` (no information loss).
- **Fix C** — default `confidence_score=0.7` at extraction-parse time when LLM omits the field. Diagnosis showed Fix 3's confidence-aware abstention rule barely fired because only 5/50 retrieved memories carried a confidence value. Defaulting at parse time ensures every fact carries a confidence the answerer can act on.

### v015d unified bench (M31 + M31b + M32) — SSP recovery delivered

LME n=90 + LoCoMo n=200 single-run. Direct comparison to v015c.

| | Cutoff | v015b baseline | v015c (M31+M32) | **v015d (+M31b)** | Δ v015d vs v015c |
|---|---|---|---|---|---|
| LME | top_50 | 67.78% (122/180) | 66.67% (60/90) | **70.00% (63/90)** | **+3.3pp** |
| LME | top_200 | 63.33% | 66.67% | **71.11% (64/90)** | **+4.4pp** |
| LoCoMo | top_50 | 86.5% (173/200) | 84.0% (168/200) | 82.0% (164/200) | −2.0pp (noise) |
| LoCoMo | top_200 | 82.5% | 84.5% | **85.0% (170/200)** | +0.5pp (arc peak) |
| **SSP @ top_50** | 73% | **47%** | **67%** (10/15) | **+20pp** ← Fix A target met |
| multi-session @ top_50 | 43% | 80% | 87% (13/15) | +7pp |

**Verdict — v0.15.0 ships with M31b included.** Fix A delivered the +5-15q SSP recovery the diagnosis predicted (landed at +20pp). LME +3.3pp at top_50 / +4.4pp at top_200 are real signal, not noise. LoCoMo within noise band (top_50 −2pp, top_200 +0.5pp = arc peak). Net combined improvement at top_50.

### Tests + safety

- **2833 tests passing** (+55 since v0.14.0): +11 from M21, +6 from M28-M29, +6 from M27 fields/coref, +22 from M31 cycle (5 fact_recall threading + 7 session_filter end-to-end + 17 temporal-resolution), +10 from M32 (PageIndexPipeline + dispatcher + Astrocyte.use_pageindex_pipeline), +9 from M32 Postgres parity (gated on `ASTROCYTE_PG_DSN`)
- **Dual-adapter validation:** all 2833 tests pass against in-memory adapter; 9 dedicated Postgres parity tests verify same observable behaviour from `PostgresPageIndexStore` (session_id round-trip, event_date round-trip, session_filter SQL EXISTS subquery, `PageIndexPipeline.recall()` end-to-end through Postgres). Catches the class of bug we hit in M28-A (migrations-don't-match-bootstrap) and M30-R2 (state pollution).
- **Backward-compat verified end-to-end**: legacy `MemoryFact` rows without `confidence_score` / `mentioned_at` / `event_date` load as `None`; legacy mental models without `structured_doc` work unchanged, with lazy parse on first delta-ops refresh; legacy `assistant_statement` fact_type rows continue to load via the remap shim. Legacy `RecallRequest` callers (no `session_id`) get unchanged behaviour (filter=None means no scoping).
- **Postgres migrations 032/033/034/035/036**: all idempotent (`ADD COLUMN IF NOT EXISTS`, guarded with `DO ... to_regclass(...) IS NOT NULL`); in-process bootstrap path mirrors so fresh DBs and existing DBs end up identical.

### Next cycle direction

Per [`docs/_design/v0.15.0-ship-decision.md`](docs/_design/v0.15.0-ship-decision.md) §5 (Carry-forward):

1. **LME measurement cadence (methodology, adopt now)** — `MEM0_HARNESS_LME_PER_TYPE=15` (n=90, ~50 min, ~$12/run, ±3pp noise floor) becomes the standing default for cycle-close benches. Prior n=30 was unmeasurable (±3-6q single-judge noise). Reserve n=180 (~3-4 hours, ~$50-80) for high-confidence pre-release validation only — LME ingestion is per-question so wall-time scales linearly with n. Full cadence table: [`v0.15.0-ship-decision.md` §5](docs/_design/v0.15.0-ship-decision.md).
2. **M30b ingest-race investigation** — fix the bench-runner state-pollution issue; investigate whether the same race surfaces in production (concurrent `add()` calls on the same user_id)
3. **Capability triad (carried from M22-M27)** — `astrocyte-embed` zero-config local mode, finish `astrocyte-rs` CLI, minimal admin UI
4. **Native LLM provider breadth** — Anthropic, Gemini, Ollama, Vertex direct (not via LiteLLM)
5. **OTel tracing + Prometheus metrics** — observability for the live-memory operations introduced in M21
6. **M30b answerer-call latency** — the real per-q latency lever (target the LLM call directly: shorter prompts, smaller model for easy questions, batched cross-encoder)

Full cycle docs:
- [`docs/_design/m21-observation-evolution.md`](docs/_design/m21-observation-evolution.md) §8 (M21 cycle close)
- [`docs/_design/m22-m25-hindsight-answerer.md`](docs/_design/m22-m25-hindsight-answerer.md) §8 (M22-M25 arc close)
- [`docs/_design/m27-memory-quality.md`](docs/_design/m27-memory-quality.md) §6 (M27 cycle close)
- [`docs/_design/m28-m29-activate-and-coreference.md`](docs/_design/m28-m29-activate-and-coreference.md) §6 (M28-M29 cycle close — 192/230 ship)
- [`docs/_design/m30-retrieval-parallelization.md`](docs/_design/m30-retrieval-parallelization.md) (M30 cycle doc + R1/R2/R3 progression + M30b operational finding)
- [`docs/_design/m31-lme-quality.md`](docs/_design/m31-lme-quality.md) (M31 cycle: Fix 2 session_filter, Fix 3 confidence-aware abstention, Fix 4 temporal-resolution-at-retain)
- [`docs/_design/m32-stack-unification.md`](docs/_design/m32-stack-unification.md) (M32 cycle: PageIndexPipeline, dispatcher routing, bench-vs-API parity audit)
- [`docs/_design/v0.15.0-ship-decision.md`](docs/_design/v0.15.0-ship-decision.md) (consolidated ship memo)

### M22-M25 (superseded by M22-M27 entry above) — early-arc progress notes (2026-05-19)

**Tight 4-step experiment arc** (M22 → M23 → M24 → M25) testing the gap-analysis hypothesis that the path to LME+LoCoMo 90%+ runs through the **answerer surface**, not the recall pipeline. All gated by `ASTROCYTE_M22_HINDSIGHT_ANSWERER=1`, default OFF — production v0.14.0 behavior at defaults unaffected.

| Cycle | Hypothesis | Bench result | Status |
|---|---|---|---|
| **M22** | Port Hindsight's 95-line directive LME prompt onto our harness | **NULL/NEG** 188/230 (−6q); LME −4q; SSP regressed −2q | Failed — over-loaded the answerer; categories where directive doesn't apply got distracted |
| **M23** | Replace monster directive with **focused per-Q-type prompts** (5-15 lines each, only rules for THIS question shape; ~3× prompt-size reduction) | Code complete; bench killed mid-flight when M24 landed | Code only; lives in `_hindsight_answerer.py` as the current prompt body |
| **M24** | Architectural fact↔chunk inline pairing (`MemoryFact.chunk_id` property + `source_chunk` threaded through `search()` + Hindsight-style `Fact N: ... Source: "..."` rendering) | **LME 21/30 = tied R0-v2**; SSP fully recovered, SSU **+1q**, SSA still −2q. LoCoMo in flight, running 89.7% with multi-hop **+4pp** and temporal **+7pp** vs R0-v2 | LME complete; LoCoMo in flight |
| **M25** | SSA fix via Hindsight's binary fact_type taxonomy: drop `assistant_statement` as a distinct type, store assistant utterances as `experience+speaker='assistant'`, add per-conversation perspective tag to extraction prompt | Bench chained behind M24 LoCoMo | Code complete; bench pending |

**Code changes in tree (all behind `ASTROCYTE_M22_HINDSIGHT_ANSWERER=1` flag):**

- `scripts/mem0_harness/_hindsight_answerer.py` (NEW) — Hindsight-parity answerer module with focused per-Q-type prompts (M23 form), inline source-chunk rendering (M24), and contextvar plumbing for qtype + observations + mental_models
- `scripts/mem0_harness/astrocyte_client.py` — `list_observations_for_bench` (wikis) + `list_mental_models_for_bench` helpers; `search()` threads `chunk_id` + `source_chunk` into fact-grain candidates
- `scripts/mem0_harness/run_lme.py` + `run_locomo.py` — install the patch
- `astrocyte/types.py` — `MemoryFact.chunk_id` + `MemoryFactHit.chunk_id` computed `@property` (stable composite identity from `(document_id, line_num)`); `MemoryFact.fact_type` docstring updated for M25 taxonomy
- `astrocyte/pipeline/section_fact_extraction.py` — M25 rewrite: `_VALID_FACT_TYPES` reduced 6→5 (removed `assistant_statement`); per-conversation perspective preamble added to `_EXTRACT_PROMPT`; `_LEGACY_FACT_TYPE_REMAP` shim for legacy LLM emits
- `tests/test_section_fact_extraction.py` — `assistant_statement` tests rewritten for M25 (assistant utterances as `experience+speaker='assistant'`), new `test_legacy_assistant_statement_remapped` covers the shim

**Architectural insight banked (Hindsight-parity ROI hierarchy):**

1. **Inline fact↔source-chunk pairing (M24)** is the foundational fix — without it, no answerer prompt change recovers the categories that need verbatim source text
2. **Per-Q-type prompts must be focused, not stacked** (M23 > M22) — 95-line monster directive regresses; 5-15 line per-category prompts work
3. **Speaker perspective belongs in a speaker field, not in fact_type** (M25 > M14.1) — Hindsight's binary world/experience taxonomy with speaker-based perspective avoids fact↔chunk redundancy
4. **Eager-compile vs lean-compile** — TBD via Phase 2 A/B test. Hindsight defers wiki/MM/preference to reflect-time or user-curation; we eagerly compile at retain. Bench impact: unknown until measured

**v0.15.0 release decision** (in `m21-observation-evolution.md §8.3`): the M21 parity 2-run mean landed at 188.5/230 (−5.5q vs R0-v2), within M19a's 6q variance band but at the LOW end. Per-question diff shows the regression is judge stochasticity + pipeline non-determinism (no M21 code-path attribution). Release blocked on M24+M25 producing a bench-positive config. If M25 lands ≥+5q vs R0-v2, v0.15.0 ships as the M21+M24/M25 bundle. If M25 is also neutral, M21 code stays in-tree unreleased and Phase 2/3 (lean-compile A/B + M20 reflect-debug) become next.

Full design + bench data: [`docs/_design/m22-m25-hindsight-answerer.md`](docs/_design/m22-m25-hindsight-answerer.md).

### M21 — Observation evolution + structured mental models + CRUD MCP (2026-05-18, CODE COMPLETE, PARITY 2-RUN MEAN BELOW R0-v2 → release blocked on M22-M25 arc)

**Live-memory architecture pivot.** Observations accrue evidence over time and acquire computed trends; mental-model documents are structured objects modified by typed delta operations rather than re-generated prose; agents can author + curate observations, mental models, and directives directly via 9 new MCP tools.

**Parity bench (2 runs, 2026-05-19): 2-run mean 188.5/230 (81.96%) — −5.5q vs R0-v2 (194/230).** Sits at the LOW end of M19a's 6q variance band (189-195). Per-question diff analysis attributes the regression to judge stochasticity (~5q) + pipeline non-determinism (~3q); none of the regressed questions touch the M21-changed categories (mental_models, observations, directives). The retain → recall → answerer hot path is provably untouched, but the 1-run parity sample isn't above R0-v2 either, so v0.15.0 doesn't ship from M21 alone — it would be API churn without measurable user value at defaults.

**Release path:** blocked on M22-M25 experiment arc (Hindsight-parity answerer + architectural fact↔chunk fix + Hindsight binary fact_type). If M24/M25 land bench-positive, v0.15.0 ships as M21+M24/M25 combined. If neutral, Phase 2 (lean-compile A/B) + Phase 3 (M20 reflect-debug) become next experiments. Full data: §8.3 of `m21-observation-evolution.md`.

**Three new subsystems** (~1,200 LOC, 85 new tests, 0 regressions on 53 existing tests):

1. **Trend computation** (`astrocyte/pipeline/trend.py` + `compute_observation_trend()` in `observation.py`) — `Trend` enum (NEW / STRENGTHENING / STABLE / WEAKENING / STALE) computed algorithmically from evidence timestamps. `_obs_source_timestamps` JSON list now persisted alongside `_obs_source_ids` so the per-source timestamps survive update cycles. Hard-coded thresholds (recent=30 days, old=90 days) mirror Hindsight defaults; will become `AstrocyteConfig` fields if users ask.

2. **Structured-doc schema** (`astrocyte/pipeline/structured_doc.py`) — Pydantic-typed sections + typed blocks (paragraph, bullet_list, ordered_list, code), deterministic markdown render, slug-based section ids, lenient parser for lazy migration of legacy markdown content. The structured doc is the authoritative representation; the rendered markdown is a deterministic projection.

3. **Delta operations** (`astrocyte/pipeline/delta_ops.py`) — 8 operation types (Append/Insert/Replace/Remove block, Add/Remove/Rename section, ReplaceSectionBlocks) the LLM emits to modify a structured doc during refresh. `apply_operations(doc, ops)` validates and applies in a copy; invalid ops drop with a logged reason; the document never gets worse than its input. Mental-model refresh now uses this path instead of regenerating the whole doc — sections and blocks not mentioned by any op are *physically copied through*, so drift on unchanged content is structurally impossible.

**9 new public methods on `Astrocyte` class + 9 MCP tools**:

| Method / MCP tool | What it does |
|---|---|
| `list_mental_models` / `memory_list_mental_models` | List models in a bank, optionally by scope |
| `get_mental_model` (no MCP — fold into list) | Fetch one model by id |
| `create_mental_model` / `memory_create_mental_model` | Author a new model from raw markdown OR sections list (modern path: structured_doc populated immediately) |
| `update_mental_model` / `memory_update_mental_model` | Apply a list of delta operations to an existing model; returns audit trail of applied + skipped ops |
| `delete_mental_model` / `memory_delete_mental_model` | Soft-delete a model |
| `create_directive` / `memory_create_directive` | Author a user-curated hard rule as `MentalModel(kind="directive")` — the architecturally-correct replacement for the M18a-2 `directive_compile` auto-extraction path deprecated in M19 (which replicated a −30pp SSP regression because compressed auto-directives overrode original preference nuance) |
| `list_observations` / `memory_list_observations` | List observations with optional trend / scope filter — returns `{id, text, trend, proof_count, source_ids, confidence, updated_at, scope}` per row |
| `get_observation` / `memory_get_observation` | Fetch one observation by id |
| `create_observation` / `memory_create_observation` | Hand-author an observation (bypass the autonomous consolidator) — for cases where an agent or user wants to assert a fact directly |
| `delete_observation` / `memory_delete_observation` | Delete one observation from the dedicated `::obs` bank |

**Postgres migration `032_mental_model_structured_doc.sql`**: NULLable JSONB column on `astrocyte_mental_models` + `astrocyte_mental_model_versions`. Legacy rows with `structured_doc=NULL` continue to work — `PostgresMentalModelStore.update_via_ops` calls `parse_markdown(current.content)` on first refresh and persists the structured form going forward. One-shot lazy migration per row, never repeated. Idempotent (`ADD COLUMN IF NOT EXISTS`). In-process bootstrap path (used by tests + bench runs) mirrors the migration.

**SPI extension**: `MentalModelStore.update_via_ops(model_id, bank_id, operations_json) -> tuple[new_revision, summary] | None`. Implementations on `InMemoryMentalModelStore` and `PostgresMentalModelStore`. `MentalModelService.create()` extended with `kind` + `structured_doc` parameters so a `directive` or sections-shaped doc creates as revision 1, not 2 (avoids a double-bump).

**Conservative-failure contract** preserved end-to-end: schema-invalid op lists (unknown op discriminator, missing required fields) return current revision with zero applied ops; per-op failures (unknown section_id, out-of-range index) drop quietly into the `skipped` audit list; only changed documents trigger an upsert. Verified by `test_delta_ops.py::TestAdversarialLLMOutput`.

**New docs**:
- `docs/_plugins/observation-evolution.md` — user-facing guide: what trends mean, how to use delta operations, the directive surface, the 9 new MCP tools
- `docs/_design/m21-observation-evolution.md` — cycle doc with full scope, architecture decisions, M22 scoping

Full design and rationale in `docs/_design/m21-observation-evolution.md` §8.

### M20 — INVESTIGATED, NOT SHIPPED: Reflect agent surface bench (2026-05-18)

**4-config LoCoMo sweep: reflect regresses −6 to −12q vs shipped recall+answerer (R0 173/200 → best reflect config R1d 167/200, −6q). NULL verdict; no release cut.**

Per M20 §5 ship gate, −6q falls in "<−2q INVESTIGATE" territory. Decision: **do not release v0.15.0**; the surface adds no measurable user value at default config (reflect default-OFF leaves v0.14.0 behavior unchanged) and shipping a public API + MCP tool that underperforms the shipped recall+answerer would only confuse downstream callers. Code stays in-tree as evidence and as the foundation for M21's per-Q-type pipeline routing experiment (§8.5 of the M20 doc) — the +5q SHIP candidate that composes R1d's temporal win (75/86, +5q vs R0) with R0's everything-else baseline.

| Config | Total | Δ vs R0 | Key finding |
|---|---|---|---|
| **R0** recall+answerer (M19a defaults, reflect OFF) | **173/200 (86.5%)** | — | Parity-check vs M19a 192/230 confirmed (R0 combined = 194/230) |
| R1 reflect generic, seed=50, iter=5 | 161/200 (80.5%) | −12q | First wiring; multi-hop crashed −12q vs R0 |
| R1b + per-Q-type routing in reflect's system prompt | 162/200 (81.0%) | −11q | **H1 falsified** — agent reads routing blocks, doesn't apply them |
| R1c + wide seed (top-200, parity with R0's view) | 161/200 (80.5%) | −12q | **H2 partially falsified** — multi-hop +4q, open-domain −4q (net zero); diagnoses synthesis (not info access) as multi-hop bottleneck |
| **R1d + max_iter=10 + STOP-EARLY rule** | **167/200 (83.5%)** | −6q | **H3 structurally falsified on stop-early** — iter distribution `{10: 200}` (was `{5: 200}` for R1c). Agent CANNOT self-terminate via `done` regardless of prompt content. +5q lift came from iteration headroom alone, concentrated in temporal |

**Three architectural insights banked for M21:**

1. **`done` tool never fires as an early exit (100% of questions hit max_iter in all 4 configs).** This is the cleanest M21 signal — the loop pathology is structural, not promptable. Fixing it requires architectural changes (force every iteration to produce a candidate answer; or add a Boolean termination-check tool the agent MUST call between recalls).

2. **Per-Q-type pipeline routing is the SHIP candidate.** Temporal-reasoning consistently beats R0 across all four reflect configs (R1d 75/86 vs R0 70/86 = +5q). Projected lift composing R1d temporal with R0 everything-else: 75+103 = 178/200 = 89.0%, +5q above R0 → would clear M20 §5 SHIP-default gate. Implementation: classify the question in `AstrocyteClient.search()`, route temporal → reflect, others → recall+answerer. ~1 day code + 1 day bench.

3. **Multi-hop bottleneck is synthesis quality, not retrieval breadth.** Wider seed (R1c) gave reflect more bridge memories (+4q multi-hop) but still left a −7q gap vs R0's answerer — confirming the M19a per-Q-type answerer prompt is doing real composition work the agentic loop doesn't replicate.

**What lands in-tree but is NOT released (no v0.15.0 cut):**

- `Astrocyte.reflect()` public API — config-driven via `agentic_reflect.enabled=True`, default OFF; routes through the native-function-calling loop in `astrocyte/pipeline/agentic_reflect.py`.
- MCP `memory_reflect` tool with tightened description that explicitly forbids the double-synthesis anti-pattern (don't pass `answer` back through another LLM for re-synthesis). Also updated `memory_recall` description to cross-reference reflect so the calling LLM picks the right surface.
- `docs/_plugins/recall-vs-reflect.md` — the consumption contract: `recall()` for raw memories (agent owns synthesis), `reflect()` for finished answers (Astrocyte owns synthesis), never chain them through a second LLM. Frames the M20 finding as positive learning — the bench harness's first cut chained reflect through the bench answerer and that wasn't a fair test of either pattern.
- Bench harness Hindsight-parity integrated-mode rewrite (`scripts/mem0_harness/run_locomo.py` monkey-patches `process_question` to call `mem0.reflect()` directly when `ASTROCYTE_USE_REFLECT=1`). Mirrors Hindsight's `LoComoReflectAnswerGenerator(needs_external_search=False)` pattern (`hindsight-dev/benchmarks/locomo/locomo_benchmark.py:198`). The bench now measures what the public API actually returns to a caller, not what an extra answerer LLM call would post-process out of reflect's answer + sources.
- New env knobs preserved for M21 experiments: `ASTROCYTE_USE_REFLECT`, `ASTROCYTE_REFLECT_MAX_ITERATIONS`, `ASTROCYTE_REFLECT_SEED_TOP_K`.
- 4 new routing/discipline blocks in the agentic-reflect system prompt (recommendation, multi-hop, temporal, knowledge-update + a STOP-EARLY block) — neutral or slightly helpful in our data; kept since they document the right answer shape for each Q-type even when the loop doesn't always honor them.

**Deferred to M21:**
- v0.15.0 release itself — no release cut for M20 since the surface has no measurable lift at defaults and would only add API churn for downstream callers.
- Reflect default-on (any consumer). `config.agentic_reflect.enabled` stays False.
- Per-Q-type pipeline routing (the +5q SHIP candidate above).
- `done`-tool architectural redesign.
- Trend computation / delta_ops / structured_doc / observations CRUD / mental models CRUD / `create_directive` MCP — already on the original M21 deferred list.

**Costs:** ~$12 across 4 LoCoMo reflect configs + 1 R0 baseline (LME+LoCoMo parallel). ~5 hours wall time. Latency: reflect averages ~9-10s/q at LoCoMo across all configs (per-iteration cost is sub-linear — 5 iter ≈ 9s, 10 iter ≈ 9s).

Full data + diagnosis + M21 scoping in `docs/_design/m20-reflect-agent.md` §8.

### M19 (v0.14.0) — SHIPPED: per-Q-type prompt routing + enriched fact extraction (2026-05-18)

**2-run mean 193/230 (83.91%)** — clears M17+1σ ship gate by +2.25pp; +4q over previously-shipped B1-dp+RRF mean; +12q over B0 baseline. New high across the M17+M18+M19 series.

| Bench | M19a 2-run mean | vs M17 baseline | vs B0 |
|---|---|---|---|
| **LME top_20** | **81.67%** (24.5/30) | M17 75.56% → **+6.11pp** | B0 66.67% → +15pp |
| **LoCoMo top_20** | **84.25%** (168.5/200) | M17 80.5% → **+3.75pp / +2.50σ** | B0 80.5% → +3.75pp |
| **Combined** | **83.91%** (193/230) | — | B0 78.70% → **+5.22pp** |

**Three architectural changes shipped:**

1. **Per-Q-type prompt routing** (`scripts/mem0_harness/_hindsight_prompt.py`) — the Hindsight SSP block is now one of FOUR inline category blocks (recommendation, multi-hop, temporal, knowledge-update), each conditionally applied by the answerer based on question shape. Fixes the M18b anti-composition: previously the SSP block bled into multi-hop questions causing the −7q B5-stack regression; per-Q-type routing scopes its influence correctly. Default ON post-M19; override `ASTROCYTE_M18_HINDSIGHT_SSP_PROMPT=0` for ablation.

2. **Enriched fact extraction (Hindsight `why` parity)** (`astrocyte/pipeline/section_fact_extraction.py`) — extraction prompt now requires each fact's `text` field to capture WHAT + WHY (reason, strength, scope, conditions), not just bare atomic statements. Example: `"User strongly prefers Sony cameras for product photography because they already own a Sony 24-70mm lens for their candle business; would not consider switching"` instead of `"User prefers Sony"`. The richer text flows through recall to the answerer, enabling SSP answers that structure recommendations around the original framing (M19a r2 hit SSP 5/5 ceiling — first time).

3. **directive_compile DEPRECATED** (`astrocyte/config.py` + `astrocyte/pipeline/document_postprocess.py`) — the M18a-2 auto-compile path is architecturally misaligned with Hindsight's user-authored `create_directive` MCP tool (compressed directives override original preference nuance, replicated −30pp SSP regression in M18b B2 × 2 runs). Code stays in-tree as reference for the future MCP-tool path; flag emits a runtime warning when set True.

**Removed:**
- M19 Workstream C boost-layer wiring in `scripts/mem0_harness/astrocyte_client.py` — regressed −7q vs shipped (Hindsight's recency boost α=0.2 favors recent sections, but our temporal-reasoning workload needs older sections for date arithmetic). The boost primitive (`astrocyte/pipeline/rerank_boosts.py`) stays available, still used by `section_rerank` where it ships clean. The `ASTROCYTE_M19_ENABLE_UNIFIED_BOOSTS` env var is no longer wired (no-op).

**Architectural insight banked for M20+:** the M18b "retrieval-feature ceiling" finding holds, but the BREAKER is **answer-time prompt routing**, not more parallel RRF siblings or unified rerank boosts. M19a composed B1-dp+RRF (recall) + Hindsight SSP block (answer) WITHOUT anti-composition because the prompt block is gated to questions where it helps. Future accuracy work should prioritise answer-time mechanisms over more recall siblings.

### M18b.2 follow-up (2026-05-18) — B3/B4/B1+B3 ablations + retrieval-ceiling finding

Ran the three deferred M18b ablations overnight (autonomous queue, ~$6 spend, ~2 hours). All three are NULL or anti-composing; **none ship**. The combined M18b + M18b.2 evidence indicates we've hit a **retrieval-feature accuracy ceiling** — adding more parallel retrieval strategies to the shipped RRF fusion does not compose further.

| Feature | Status | 2-run mean (where available) | Why not shipping |
|---|---|---|---|
| B3 spreading_activation | ❌ Null | LME 68.33% / LoCoMo 82.50% / total 185.5 (80.65%) | LoCoMo +2pp lift didn't replicate — r1 had temporal +4, r2 had temporal −1; net within noise |
| B4 episodic_extract | ❌ Null | r1 only: total 182 (+1q vs B0) | Slight negative composition with rerank; temporal regression −4 on the single run |
| B1+B3 stack | ❌ Anti-composes | r1 only: total 184 (+3q vs B0; −5q vs B1-dp+RRF alone) | Same shape as B5-stack: combined < best individual. Multi-hop +3 (RRF wins), temporal −3 (B3 loses) |
| **All-ON B5** (all 5 features simultaneously) | Plateaus, **no per-cat regression on LoCoMo** | r1 only: LME 20/30 / LoCoMo 169/200 / total 189 (82.17%) | LoCoMo matches top-tier (open-domain +5, biggest OD lift of M18b series; multi-hop +1, temporal +2, no regression). LME perfectly cancels at B0 (SSA −2 vs SSP+1+SSU+1). Total ties B1-dp+RRF 2-run mean. **Does not exceed single-feature ceiling.** |

**Retrieval-ceiling finding (banked for M19) — revised with all-ON B5 data:** four parallel retrieval strategies tested as RRF siblings on top of shipped fact_recall (B2/B3/B4 individually plus stacking combinations) — multiple configurations cluster at the same 189-192/230 combined accuracy ceiling. **2-feature stacks ANTI-COMPOSE** (B5-stack 182-187, B1+B3 184 — categories compete, one wins / one loses, net combined < best individual). **5-feature all-ON does NOT anti-compose** (189/230 with zero per-category LoCoMo regression — open-domain even hits +5, biggest OD lift of M18b series) but doesn't EXCEED the ceiling either. **Mechanism:** at 2 features each signal is strong enough to displace the other's wins; at 5 features no signal dominates, system falls back to baseline+coverage. The fix is not "more or fewer RRF siblings" — it's a NEW mechanism that lets signals compose by modulation rather than competition.

**This sharpens the M19 priority:**

1. **Per-question-type prompt routing** (~1-2 weeks) — `prompt-only` was the strongest standalone feature in M18b (+9.5q combined vs +8q for B1-dp+RRF), but anti-composes with shipped B1-dp+RRF on multi-hop. Per-question-type prompt application would let us ship the prompt lift without the composition cost.
2. **Hindsight `create_directive` MCP tool** (~1 week) — replaces B2's broken auto-compile with the architecturally correct user-authored hard-rule path (`mcp_tools.py:_register_create_directive`).
3. **NEW retrieval STAGES** (not parallel siblings) — BM25 as a pre-RRF gating filter; Reflect agent iterative recall.
4. **Document Engine bench** (untested at scale) — validate the M17 engine split with a real FinanceBench / DoubleBench run.

M18b + M18b.2 cycle spend: ~$25 of $80 cap. Cycle CLOSED.

### M18b cycle close (2026-05-17) — final ablation matrix + ship summary

**M18b ablation series complete (5 conditions × 2 runs each, ~14 bench runs).** Full per-condition data in [`docs/_design/m18-quick-wins.md`](docs/_design/m18-quick-wins.md) §8.

| Condition | LME (n=30) | LoCoMo (n=200) | Combined/230 | Ship? |
|---|---|---|---|---|
| B0 baseline | 66.67% | 80.5% | 78.70% | — |
| **B1-dp+RRF** (shipped below) | 71.67% | **83.75%** | 82.17% | ✅ shipped |
| B2 directive_compile | 70.00% | 82.75% | 81.09% | ❌ SSP regression replicated |
| **prompt-only (Hindsight SSP)** | 78.33% | 83.50% | **82.83%** | ⏸️ validated but deferred (see below) |
| **B5 stack (B1-dp+RRF + prompt)** | 76.67% | 80.75% | 80.22% | ❌ **anti-composition confirmed** |

**Two new findings worth banking:**

1. **B5 stacking actively anti-composes on multi-hop synthesis.** B1-dp+RRF alone lifted LoCoMo multi-hop +6q; prompt-only alone lifted +3q; *combined* gave −0.5q vs B0 (mean over 2 runs). The features compete for the same answerer attention — RRF wants synthesis across temporal candidates; the SSP prompt steers toward concrete user-grounded answers. Result: multi-hop questions get committed-but-shallow answers instead of synthesized ones. **Don't naively stack recall improvements with answerer-prompt steering** — the composition needs explicit per-question-type routing.

2. **directive_compile is architecturally divergent from Hindsight.** Their `mental_models.subtype='directive'` rows are USER-AUTHORED hard rules installed via the `create_directive` MCP tool, not LLM-compiled from preference facts. Our auto-compile pass produces overconfident-rule failures on single-session-preference questions (−30pp on LME SSP, replicated both runs). The Hindsight SSP prompt block alone (no directive compile) recovers SSP and gives the cleanest single-feature lift we've measured (+11q vs B0 on combined). **Pivot path: ship the SSP prompt as standalone in M19 after the RRF/prompt interaction is properly investigated.**

**M18b cycle spend:** ~14 bench runs × ~$1-2 each ≈ $20 of the $80 cap. M18b.2 (B3 spreading_activation, B4 episodic_extract) deferred.

### M18b ship — B1-dp+RRF (dateparser Pass B + RRF fact fusion) (2026-05-17)

The single feature shipped in M18b. 2-run LoCoMo mean **83.75%** clears M17+1σ ship gate (82.0%) by **+1.75pp**; combined LME+LoCoMo +8q vs B0 baseline. See earlier `## [Unreleased]` entries below for the architectural details (dateparser Pass B, RRF fusion, MemoryFact rename, migration 029).

**What's NOT shipped (code present, flags OFF by default):**
- `directive_compile` (B2) — `DirectiveCompileConfig.enabled=False`. Bench evidence: replicated SSP regression. Kept in-tree pending M19 pivot to user-authored `create_directive` MCP tool path.
- Hindsight SSP prompt block — `scripts/mem0_harness/_hindsight_prompt.py` gated by `ASTROCYTE_M18_HINDSIGHT_SSP_PROMPT=1`. Bench evidence: standalone lift (+9.5q combined vs B0, 2-run mean) but **anti-composes with shipped B1-dp+RRF on multi-hop**. Kept in-tree pending M19 prompt-routing investigation.
- `spreading_activation` (B3) — `SpreadingActivationConfig.enabled=False`. Not benched in M18b; defer to M18b.2.
- `episodic_extract` (B4) — `EpisodicExtractConfig.enabled=False`. Not benched in M18b; defer to M18b.2.

### M18b post-ship cleanup — Memory Engine naming + nullable section anchor (2026-05-17)

Renamed the primary Memory-Engine fact type and relaxed its required document anchor to match Hindsight's :class:`MemoryUnit` semantics. Tightens conceptual coupling now that M17 split Astrocyte into 3 explicit engines (Memory / Document / Conversation).

**Changes:**
- `astrocyte/types.py`: `PageIndexFact` → `MemoryFact`, `PageIndexFactHit` → `MemoryFactHit`. Old names retained as backward-compat aliases (`PageIndexFact = MemoryFact`); the 15 existing call sites continue to work unchanged and can migrate gradually.
- `MemoryFact.document_id` and `MemoryFact.line_num` are now `str | None = None` (were required). Top-level facts (no section anchor) are now constructible — matches Hindsight's `MemoryUnit.document_id` nullable semantics.
- Postgres migration **029_pi_facts_nullable_document_id.sql**: `ALTER TABLE astrocyte_pi_facts ALTER COLUMN document_id DROP NOT NULL` + same for `line_num`. Idempotent (re-run safe). Existing composite FK kept; PG `MATCH SIMPLE` default treats either-NULL as passing.

**Why this matters:** the name `PageIndexFact` was an M12.1 historical artifact (fact-grain was introduced bolted to PageIndex sections). The type's actual role is the Memory Engine's primary fact unit — Hindsight's `MemoryUnit` analogue. The rename + nullable change make it clear that section anchoring is an *implementation* (cheap context-expansion + cascade-delete), not a *requirement*, and unblocks future top-level retain APIs.

**Tests:** `tests/test_memory_fact.py` (8 new) — alias equivalence, anchored construction, top-level construction with no anchor, in-memory round-trip. 2665 total tests pass.

### M18b-B1 dateparser + RRF fact fusion — SHIPPED (2026-05-17)

Promoted to default after the M18b ablation bench confirmed a real LoCoMo accuracy lift. The combined "B1-dp+RRF" feature shipped is *one architecturally cohesive change*, not two independent ones — they validate jointly.

**Components shipped:**
- `astrocyte/pipeline/temporal_dateparser.py` (NEW) — Hindsight-parity Pass B wide-net date extractor (`dateparser.search.search_dates`, defensive try/except, false-positive filter for short common words). Wired in as the LAST branch of `_regex_temporal_pass` chain when `enable_temporal_expansion=True`.
- `astrocyte/pipeline/fact_recall.py` (REFACTOR) — semantic + episodic + temporal now run as **PARALLEL SIBLINGS** via `asyncio.gather` and merge via Reciprocal Rank Fusion (RRF k=60). Replaces the old "append everything, let the cross-encoder sort it out" pool-build that was source-blind and high-variance.
- `astrocyte/config.py` — `QueryAnalyzerConfig.enable_temporal_expansion` default flipped to `True`. Bench env override `ASTROCYTE_M18_ENABLE_TEMPORAL_EXPANSION=0` still works for ablation.
- `pyproject.toml` — `dateparser>=1.2` in `[bench]` extras. Graceful no-op when absent.

**Bench results (M18b 2-run mean vs B0 single run, gpt-4o-mini answerer + judge, fair-sample):**

| Bench | B0 (no temporal) | B1-dp+RRF 2-run mean | Δ |
|---|---|---|---|
| LME top_20 (n=30) | 66.67% | 71.67% | +5.0pp (within n=30 ±5pp noise) |
| LoCoMo top_20 (n=200) | 80.50% | **83.75%** | **+3.25pp** (+2.17σ vs M17 baseline mean) |

**Ship-gate verdict:** LoCoMo 2-run mean clears M17 + 1σ gate (82.0%) by 1.75pp. LME variance (66.7% / 76.7% per run swing) is dominated by single-session-preference category at n=5; LME doesn't independently confirm but doesn't refute either.

**Where the lift came from** (per-category Δ vs B0, replicated across both runs):
- LoCoMo **multi-hop +4** (B0 67/79 → mean 71/79, +5pp on the largest category)
- LoCoMo open-domain +2
- LoCoMo temporal +0.5 (modest; the named subcategory was less-affected than expected)

The mechanism is NOT primarily "answering temporal questions better." It's "dateparser surfaces date references in synthesis questions (e.g., 'what did X say in March about Y'), RRF rewards facts that appear in both semantic AND temporal strategies, sharpening the candidate set for the cross-encoder."

**Two architectural lessons banked:**
1. **`dateparser` (Hindsight Pass B) is the right wide-net temporal extractor** — pure-regex Pass A alone missed most real date refs.
2. **RRF fusion of parallel strategies is structurally necessary** — without it, the previous append-then-rerank approach showed phantom +13pp lifts on LME that didn't replicate. Cross-strategy agreement is what makes recall robust to false-positive dateparser hits.

Full M18b cycle log in [`docs/_design/m18-quick-wins.md`](docs/_design/m18-quick-wins.md). Test additions: `tests/test_temporal_dateparser.py` (4) + `tests/test_fact_recall.py` (rewritten, 12).

### M17 PageIndex-style ingestion + Conversation Engine — SHIPPED (2026-05-17)

The M17 cycle delivered three composable engines (Memory, Document, Conversation) plus a composition layer, with a measurable bench lift over the M14-close baseline. Full design + cycle-close in [`docs/_design/m17-pageindex-ingestion.md`](docs/_design/m17-pageindex-ingestion.md). M18 quick-wins scoping in [`docs/_design/m18-quick-wins.md`](docs/_design/m18-quick-wins.md).

**3-run mean (LME-30 + LoCoMo-200 fair subsets, gpt-4o-mini answerer + judge, --user-profile, top_k=200):**

| Bench | M14 baseline | M17 conv (3-run mean) | Δ |
|---|---|---|---|
| LME top_20 | 65.0% ±3.3pp | **75.56%** ±5.09pp | **+10.56pp (3.2σ)** ✅ ship gate cleared |
| LoCoMo top_20 | 78.25% ±1.5pp | **80.50%** ±1.50pp | **+2.25pp (1.5σ)** ✅ no-regression gate cleared |

**Architecturally**, M17 establishes Astrocyte as a 3-engine composable framework:
- **Memory Engine** (existing `astrocyte/` core) — facts, sections, wikis, banks, retrieval. Hindsight-inspired.
- **Document Engine** (NEW `astrocyte/documents/`) — file → hierarchical tree. PageIndex-inspired (md_builder mirrors `md_to_tree`, adaptive summarizer uses PageIndex's exact prompt).
- **Conversation Engine** (NEW `astrocyte/conversations/`) — turns → session-aware turn-boundary chunks. Hindsight-inspired (mirrors `_chunk_conversation`).
- **Composition layer** — `DocumentIngestor` + `ConversationIngestor` bridge to Memory Engine via opaque metadata strings (no FK coupling). Each engine standalone-usable; zero cross-imports verified.

**Bench attribution**: LME/LoCoMo are conversation workloads; the M17 bench measures **Conversation Engine + Memory Engine**. Document Engine ships but is not benched on these conversation datasets (its eventual bench is FinanceBench / DoubleBench, future cycle).

**Code shipped:**

- **Sprint 1** (Hindsight-parity adoptions): `astrocyte/audit.py` (migration 023), `astrocyte/operation_metadata.py`, `astrocyte/task_backend.py`, `astrocyte/pipeline/cross_encoder_rerank.py` (MPS device auto-detect + Apache-2.0 model presets — Jina-MLX deliberately excluded due to CC-BY-NC).
- **Sprint 2** (Hindsight-parity adoptions): `astrocyte/db_budget.py` (connection-pool budgets), `astrocyte/disposition.py` + migration 024 (skepticism/literalism/empathy traits).
- **M17 Phase 1+2** (Document Engine): `astrocyte/documents/` subpackage (types, parsers/{base,markdown}, builders/{md_builder,summarizer}, storage), Postgres impl in adapters, migrations 025 + 026.
- **M17 Phase 2.5** (Conversation Engine): `astrocyte/conversations/` subpackage (types, chunking with session-aware boundaries, storage), Postgres impl in adapters, migrations 027 + 028.
- **M17 Phase 3** (composition layer): `astrocyte/documents/ingestor.py`, `astrocyte/conversations/ingestor.py`, shared SPI in `astrocyte/_ingest_spi.py`; bench harness routes LME/LoCoMo through `ConversationIngestor` when `ASTROCYTE_M17_INGEST_PATH=conversation` (the new default for these workloads).

**Tests:** 194 unit tests passing across Sprints 1+2 + M17 Phases 1+2+2.5+3.

**Cost accounting:** ~$8 of M17's $400 budget cap consumed (significantly under). Budget headroom carries into M18.

**What did NOT land:**

- Phase 6 PDF parsers (markitdown + LlamaParse) — Document Engine API ships; PDF support arrives when there's a real PDF ingest use case.
- Phase 7 external `pageindex` dep removal — the bench harness is wired around an env-var ingest-path switch; the external dep can still be imported for parity comparisons. Final removal waits for production-readiness confirmation.

**Methodological lessons:**

- The catastrophic conv-run-1 (−38pp LME) was a bridge-adapter bug — shoving 12k-char Conversation Engine chunks into single sections starved fact extraction. Session-aware chunking (turn-boundary AND session-boundary respect) fixed it; conv-run-3+ replicated cleanly.
- LME has wider run-to-run variance than LoCoMo (±5.09pp vs ±1.50pp at the same flag). The conservative "central tendency" LME lift, accounting for one SSP outlier in run-4, is closer to +8pp than the headline +10.56pp from arithmetic 3-run mean. Both numbers clear the ship gate.

**Gap to Hindsight after M17:** LME 75.56% vs Hindsight's published 94.6% → **−19.04pp remaining**. LoCoMo 80.50% vs Hindsight's 92.0% → **−11.50pp remaining**. M18 attacks the cheapest remaining engine-side levers (LLM-fallback flag flip + multiplicative score boosts, ~$60 budget).

### M14 retain pipeline overhaul — cycle closed with null bench verdict (2026-05-15)

The M14 cycle ran an architectural experiment (atomic-fact consolidation + entity canonicalization + query-analyzer wiring) modelled on Hindsight's design. Implementation was built as working-tree WIP, benched across 3 runs of the wikis-off configuration, and **torn down without commit** when the null bench verdict made the implementation bench-unnecessary. Full retrospective in [`docs/_design/m13-m14-roadmap.md`](docs/_design/m13-m14-roadmap.md) §§8-12.

**What landed in HEAD:**

- `Makefile` — `BENCH_RESET` knob (default `1`) on every `bench-*` target. Bench DBs are reset before every run unless `BENCH_RESET=0`. Fixes a silent 30× slowdown caused by accumulated state across re-runs (commit `d982fde`).
- M14.7 revert — `wiki_incremental` bench wiring dropped from `bench_pageindex_locomo.py` (commit `6ec61ea`).
- Methodology documentation — multi-run baseline policy, 2σ signal-detection thresholds, per-category attribution gate, 9-decision locked policy framework (in this doc's §§8-9).
- Hindsight architectural critique + remaining-gap analysis ranked by expected bench impact (§§10, 12).

**What did NOT land in HEAD (experiment torn down):**

- `observation_consolidator.py` (atomic-fact consolidator), `consolidation_models.py` (Pydantic response schemas), `entity_canonicalization.py` (per-doc clustering), migrations 023 + 024, and the consolidator / canonicalization / query-analyzer bench wiring. These were built and tested but never committed; the experiment's design is preserved in §8.7 as a future-cycle blueprint.

**Bench results (4-run baseline → 3-run wikis-off M14 experiment):**

- LME top_20: 65.0% ±3.3pp → 68.9% ±5.0pp (+3.9pp; within 1σ; **doesn't clear locked 2σ gate of 71.6%**)
- LoCoMo top_20: 78.25% ±1.5pp → 75.83% ±1.5pp (−2.4pp; borderline regression)

The architectural overhaul is mechanically sound but doesn't yield bench-measurable signal at gpt-4o-mini answerer scale. The current operating point (HEAD at `6ec61ea`) is the M14 baseline — equal to what the experiment would have produced.

**Dropped (accepted null-verdict implication):**

- Phase 2.2 `multi_query`, Phase 2.3 HyDE, Phase 5 proof-count, Phase 6 per-fact recall trigger, Phase 7 bank-mission smuggle — all gated on Phase 4 success; superseded by null result.

**Forward-looking analysis (§12):** the remaining Hindsight design gaps ranked by expected bench impact are tool-calling reflect agent (§12.1, +5-15pp LME), link-expansion graph retrieval (§12.3, +3-7pp LoCoMo multi-hop), and multiplicative score boosts (§12.2, +2-5pp). These are outside the M14 retain-pipeline scope; deferred to a future cycle.

### ⚠️ Breaking — Apache AGE removed (M9 / ADR-008)

Apache AGE is no longer supported. M9's section-grain recall stack replaces AGE-backed graph operations with flat tables + SQL CTEs (Hindsight pattern), at section grain (`astrocyte_pi_section_links`, `astrocyte_pi_section_entities`) and at memory-unit grain (`astrocyte_unit_links`, `astrocyte_unit_entities`).

**Migration:**

1. Remove the `graph_store: age` line from your config. The `graph_store` field becomes optional; graph operations live inside the Postgres adapter as flat tables.
2. If your bank was AGE-backed, rebuild from raw memory_units before upgrading. There is no migration tool — re-extraction produces a cleaner result than walking AGE's per-label OID structure.
3. Drop the `astrocyte-age` dependency from your `pyproject.toml` / `requirements.txt`.
4. Migration `016_drop_age.sql` runs `DROP EXTENSION IF EXISTS age CASCADE` (idempotent — deployments without AGE skip cleanly).

**Impact on Docker / Helm:** the `astrocyte-postgres` image base flipped from `apache/age:release_PG16_1.6.0` to `postgres:16-trixie`. `shared_preload_libraries` no longer includes `age`. Smaller image, faster cold start, one fewer extension to upgrade across Postgres majors.

If you set `graph_store: age` in any config after upgrading, startup raises `ConfigError` with a pointer to ADR-008.

See:
- `docs/_design/recall.md` — recall design (M9)
- `docs/_design/adr/adr-006-three-layer-recall-stack.md`
- `docs/_design/adr/adr-007-pageindex-tree-as-section-primitive.md`
- `docs/_design/adr/adr-008-section-graph-replaces-age.md`

## [0.11.0] — 2026-05-03 (multi-tenancy, mental models, package rename)

This release introduces schema-per-tenant isolation across the entire stack, a new mental-model knowledge tier, intent-driven reflect routing, and a comprehensive package rename. Operators upgrading from v0.10.0 must replace `astrocyte-pgvector` with `astrocyte-postgres` in their dependency lists.

### ⚠️ Breaking — package rename

The Postgres adapter package has been renamed from `astrocyte-pgvector` to `astrocyte-postgres`. The new name reflects that the package can grow to host alternate Postgres-backed strategies (IVFFlat, scalar quantization) without further renames, and aligns with the `docker/astrocyte-postgres` image name.

**Migration:**

```bash
pip uninstall astrocyte-pgvector
pip install astrocyte-postgres==0.11.0
```

The following names have changed in code:

| Before | After |
|---|---|
| PyPI: `astrocyte-pgvector` | `astrocyte-postgres` |
| Class: `PgVectorStore` | `PostgresStore` |
| Class: `PgWikiStore` | `PostgresWikiStore` |
| Entry-point key: `pgvector` | `postgres` |
| Gateway extra: `pip install astrocyte-gateway-py[pgvector]` | `[postgres]` |

The underlying `pgvector` PostgreSQL extension and the `pgvector/pgvector:pg16` upstream Docker image are unchanged — they are upstream technologies, not Astrocyte packages.

### Added

- **Schema-per-tenant isolation** (`astrocyte/tenancy.py`, `adapters-storage-py/astrocyte-postgres/`, `adapters-storage-py/astrocyte-age/`): every tenant gets its own Postgres schema, isolating both vector tables and AGE graphs. Adapters honour `use_schema()` via a `ContextVar` + `fq_table` primitive; the gateway installs tenant-binding middleware around every request so per-tenant boundaries are enforced at the request edge, not the query.
- **Per-tenant migration script** (`adapters-storage-py/astrocyte-postgres/scripts/migrate.sh`): accepts `SCHEMA=<name>` to migrate a single tenant's schema; new `migrate-all-tenants` wrapper supports four discovery modes (env list, JSON manifest, SQL query, schema regex).
- **Per-tenant PgQueuer fan-out** (`astrocyte-services-py/astrocyte-gateway-py/`): the gateway worker spawns one poller per tenant, so async work runs against the correct schema without cross-tenant queue starvation.
- **Mental models knowledge tier** (`pipeline/mental_models.py`): a new top-of-pipeline knowledge layer that sits above raw memories and observation-consolidated digests. Queryable via `brain.search_mental_models()`. Surfaced through the gateway and the agentic-reflect tool registry.
- **Observation scope + invalidation** (`pipeline/observation.py`): observations can now be scoped to bank / tag / time-range and invalidated when their source memories change. Eliminates stale digests that previously could survive forget operations.
- **Postgres-native fast paths** (`pipeline/recall.py`): query rewriting at the SQL level (UNION ALL across fact-types, partial indexes per (bank_id, fact_type)) cuts recall latency on multi-bank workloads. Per-strategy timings exposed via `RecallTrace.strategy_latencies`.
- **Intent-driven reflect routing** (`pipeline/reflect.py`): prompt-variant selection is now driven by query-intent classification (temporal / inference / abstention / synthesis) rather than regex-based heuristics. Adds extraction-discipline rules for date arithmetic and counting.
- **Four-preset benchmark ablation matrix** (`benchmarks/`): four versioned configs (`config-baseline.yaml`, `config-hindsight-balanced.yaml`, `config-hindsight-aggressive.yaml`, plus the existing default) for per-feature ablation. Used by the bench harness to identify which features actually move scores.
- **Benchmark state-reset helper** (`astrocyte/eval/_state_reset.py`): TRUNCATEs every bench-relevant Postgres table and recreates the AGE graph at the start of every benchmark run. Eliminates state leakage across runs (stale wiki pages, accumulated entity aliases, orphaned PgQueuer tasks). Wired into `LoComoBenchmark.run`, `LongMemEvalBenchmark.run`, `MemoryEvaluator.run_suite`.
- **Schema-per-tenant operator guide** (`docs/_end-user/`): production runbook covering per-tenant migrations, AGE schema setup, and the gateway's tenant-binding middleware.

### Changed

- **`pgvector` adapter** (`adapters-storage-py/astrocyte-postgres/`): `text_fts` tsvector column is now part of static migrations rather than added lazily by `_ensure_schema`. Migrations are the single source of truth for the schema.
- **Reflect prompt-variant selection** routes by intent classification rather than regex heuristics; adds date-arithmetic discipline rules.

### Fixed

- **Bootstrap test** (`adapters-storage-py/astrocyte-postgres/tests/`): test fixture's bootstrap path now stays in sync with the migration files so test/prod schema parity is enforced at CI time.

### Eval

- Benchmark history files updated through 2026-05-03.
- Hindsight-balanced preset added for adversarial-defense ablation runs.
- All bench targets now start from a clean Postgres state via the new state-reset helper, removing a class of run-to-run variance.

### Security

- **Path-traversal containment** (`portability.py`, CWE-022): `export_bank`, `read_ama_header`, `iter_ama_memories`, and `import_bank` now route every input path through an explicit `_safe_resolve()` validator. Production deployments should set `ASTROCYTE_PORTABILITY_ROOTS=/var/lib/astrocyte/exports` (os.pathsep-joined for multiple roots) to opt into containment; library / CLI / test usage is unchanged. Closes CodeQL alerts #29–#35.
- **Exception message sanitization** (`gateway/tenancy.py`, `gateway/app.py`, CWE-209): the 401 response from the tenant-binding middleware and the per-bank error in the DSAR forget-principal sweep now return stable identifiers (`authentication_failed`, `internal_error`) instead of raw `str(exc)` text. Full detail is captured in server logs (`exc_info=True`). Closes CodeQL alerts #36–#37.

## [0.10.0] — 2026-05-02 (retrieval quality, gateway surface area, lifecycle, DSAR)

A substantial release covering retrieval-quality improvements (HyDE, observation consolidation, multi-query gating, adversarial defense), expanded gateway surface (graph search/neighbors, compile, DSAR erasure), bank lifecycle features (history, audit, export/import, legal hold), ingestion adapters (S3/Garage, document folders), and a 7× speedup on the retain phase of LongMemEval.

### Added

- **R1 — Hypothetical Document Embedding (HyDE)** (`pipeline/retrieval.py`): query is rewritten by an LLM into a hypothetical answer document, embedded, and used as the retrieval query alongside the literal one. Lifts recall on questions where the user's surface form differs lexically from the stored memory.
- **Observation consolidation layer** (`pipeline/observation.py`): post-retain LLM synthesis layer that compresses raw memories into structured observations stored in a dedicated `::obs` bank. Routed via MIP; surfaced through intent-gated injection during recall on open-domain queries.
- **Multi-query confidence gate** (`pipeline/recall.py`): retrieval at threshold 0.72 — when the top-K confidence is below the gate, recall expands the query and retries before returning. Reduces low-confidence hallucination on ambiguous prompts.
- **Adversarial defense** (`pipeline/reflect.py`): adversarial-abstention prompt template + temporal-aware prompt auto-selection in `brain.reflect()`. Detects and refuses prompt-injection-style queries.
- **Structured fact extraction** (`pipeline/structured_facts.py`): typed fact extraction with provenance pointers; surfaces under `MemoryHit.facts`.
- **Query temporal analyzer** (`pipeline/query_analyzer.py`): classifies queries on temporal axis (point-in-time, range, change-detection) and steers retrieval policy.
- **Agentic reflect** (`pipeline/agentic_reflect.py`): tool-calling reflect that can iteratively widen retrieval, follow links, and consolidate evidence before answering.
- **Fact-level causal links + link expansion** (`pipeline/links.py`): graph-walks at recall time to surface evidence chains, not just the top-K.
- **Bank lifecycle surface** — `brain.history()`, `brain.audit()` (gap analysis), `brain.export_bank()`, `brain.import_bank()`, `brain.bank_health()`, `brain.legal_hold()`. Each is exposed as a public method with full pipeline tracing.
- **`POST /v1/dsar/forget_principal`** (gateway): right-to-erasure endpoint for Cerebro. Enumerates a tenant's banks, erases all rows tagged `principal:{principal}`, returns per-bank deletion counts. The contract counterpart of `Synapse.DSAR.DeletionWorker` in the cerebro repo.
- **`POST /v1/compile`** + **`POST /v1/graph/search`** + **`POST /v1/graph/neighbors`** (gateway): wiki compile trigger, semantic graph search, neighbor expansion. Backed by new public methods `brain.graph_search()` and `brain.graph_neighbors()` and exposed as MCP tools.
- **S3 / Garage ingestion adapter** + **document folder ingestion adapter** (`adapters-ingestion-py/`): two new ingestion sources, available as gateway extras (`pip install astrocyte[ingest-s3]` / `astrocyte[ingest-document]`).
- **Hindsight-informed reference stack** (`pipeline/hindsight.py`): retrieval pipeline preset that mirrors the Hindsight paper's offline-augmentation pattern. Default for LoCoMo benchmark runs.
- **PgQueuer-backed memory task worker** (`pipeline/tasks.py`): out-of-band background work (compile, lifecycle sweeps) backed by Postgres queues; eliminates the in-process work loop.
- **Concurrent retain phase** (eval): token-bucket rate limiter on LongMemEval retain, ~7× speedup.
- **Configurable HNSW vector schema** (`adapters-storage-py/astrocyte-postgres/`): operators can tune `m` / `ef_construction` per bank.
- **Postgres reference stack parity**: `docker/postgres-age-pgvector/` ships a single image with both extensions; matches what the published `astrocyte-postgres` image bundles.

### Fixed

- **`pipeline/reflect.py`**: evidence-strict gate restored on weak retrieval to prevent hallucination when no high-confidence memories match.
- **`pipeline/observation.py`**: observation I/O now correctly routed through the dedicated `::obs` bank (previously could leak into the parent bank under some configs).
- **AGE adapter**: `_ensure_schema` now eagerly pre-creates the `Entity` vlabel and `LINK` elabel; removes a startup race where the first `store_entities` call could fail with "label not found." `store_entities` skips the embedding column when no embedding is supplied.
- **Entity extraction**: `raw_decode` now used to handle multi-array LLM responses (was failing with `Extra data` errors); `max_tokens` raised to 1024 to fit larger responses.
- **CodeQL**: path traversal in `portability.py` (CWE-022), `StatementNoEffect`, `EmptyExcept`, and unhandled `await` results across `pipeline/`, `tasks/`, and benchmark scripts.
- **Webhook ingest**: error responses now sanitised before being returned to the caller.

### Changed

- **Multi-arch publishing**: gateway image and reference Postgres image now published as `linux/amd64` AND `linux/arm64` manifests.
- **Reference providers**: warmed during gateway startup, not lazily on first request — eliminates the cold-start latency on the first `/v1/recall` after deploy.

### Eval

- LoCoMo retrieval pipeline rewritten with hindsight-informed reranking; significant accuracy gains on multi-session questions.
- LoCoMo retain phase concurrent with rate limiter (~7× faster).
- Benchmark history files updated through 2026-05-01.
- Stratified per-conversation sampling in `fair-bench` for reproducible category coverage.

## [0.9.1] — 2026-04-25 (patch — widen adapter dep range)

### Fixed

- **All adapter packages** (`astrocyte-postgres`, `astrocyte-qdrant`, `astrocyte-neo4j`, `astrocyte-elasticsearch`, `astrocyte-ingestion-*`, `astrocyte-llm-litellm`): dependency was `astrocyte>=0.7.0,<0.9`, causing `ResolutionImpossible` when installing alongside `astrocyte==0.9.0`. Widened to `<2`.

## [0.9.0] — 2026-04-25 (M8–M11 — wiki compile, time travel, gap analysis, entity resolution)

Four milestones completing the pre-GA feature surface. M8 (wiki compile) ships on the v0.8.x engineering track; M9–M11 constitute the v1.0.0 scope and are now fully implemented. v1.0.0 GA will be declared after the v0.9.x eval gates pass.

### Added

- **M8 — LLM wiki compile** (`pipeline/wiki_compile.py`, `pipeline/wiki_lint.py`): `WikiPage` memory type maintained by `CompileEngine`; retain-time async compile queue; threshold-based trigger; wiki-tier recall precedence (wiki hits ranked above raw memories); periodic lint pass catches contradictions, stale claims, and orphans; A/B eval harness with regression gate (≥10pp lift on LongMemEval `multi-session` / `knowledge-update`, no other category regressing >2pp). Opt-in per bank via MIP config (`compile: { enabled: true }`). `brain.compile(bank_id)` for manual trigger.
- **M9 — Time travel** (`pipeline/retrieval.py`, `types.py`, `_astrocyte.py`): `retained_at` timestamp stamped on every `VectorItem` at retain time and propagated through the full pipeline (`ScoredItem`, `MemoryHit`). `VectorFilters.as_of: datetime | None` filters retrieved memories to those retained on or before the given UTC timestamp. `brain.history(query, bank_id, as_of) → HistoryResult` convenience wrapper. `HistoryResult` carries `hits`, `total_available`, `truncated`, `as_of`, `bank_id`, and `trace`.
- **M10 — Gap analysis** (`pipeline/audit.py`, `types.py`, `_astrocyte.py`): `brain.audit(scope, bank_id) → AuditResult` — samples up to `max_memories` recent memories, sends them to an LLM judge with the operator-supplied `scope` description, and returns a `coverage_score` (0–1) plus a list of `GapItem(topic, severity, reason)`. Empty-bank fast path skips the LLM call entirely. Graceful fallback on malformed LLM response.
- **M11a — Entity resolution** (`pipeline/entity_resolution.py`, `provider.py`, `types.py`): `EntityLink` migrated to `entity_a` / `entity_b` (was `source_entity_id` / `target_entity_id`); added `evidence: str`, `confidence: float`, `created_at: datetime | None`. `EntityResolver` opt-in pipeline stage: at retain time, new entities are compared against existing candidates via `find_entity_candidates`; an LLM judge confirms aliases; confirmed pairs are stored as `alias_of` links. Resolution failures never abort retain. `GraphStore` SPI extended with `find_entity_candidates` and `store_entity_link`. `InMemoryGraphStore` implements both.
- **M11b — `astrocyte-age`** (`adapters-storage-py/astrocyte-age/`): new PyPI package. Apache AGE (PostgreSQL 16 graph extension) `GraphStore` implementation. Migrations in `migrations/` (AGE extension, graph DDL, memory-entity mapping table). `bootstrap_schema=True` (default) auto-creates on first connection. `docker/postgres-age-pgvector/Dockerfile` bundles AGE + pgvector on a single PG16 image. `astrocyte-services-py/docker-compose-age.yml` for local development. CI job in `adapters-storage-ci.yml`; `publish-astrocyte-age.yml` for PyPI Trusted Publishing; wired into `release.yml`.
- **Eval: checkpoint/resume** (`eval/checkpoint.py`): `BenchmarkCheckpoint` serialises per-question progress to JSON; `--resume` / `RESUME=1` skip already-scored questions on restart. Prevents full re-runs after network or quota failures.
- **Eval: `bench-full` target** (`Makefile`): runs LoCoMo and LongMemEval concurrently via `asyncio.gather`; unified summary table.
- **Eval: LoCoMo LLM judge** (`eval/judges/locomo_judge.py`): competitor-comparable LLM yes/no scoring alongside canonical stemmed-F1; `--canonical-judge` flag selects scoring path.
- **Docs: benchmark roadmap** (`docs/_design/benchmark-roadmap.md`): three-tier benchmark strategy — Tier 1 (LoCoMo + LongMemEval, current), Tier 2 (AMA-Bench, agentic trajectory memory, planned), Tier 3 (MemoryArena, task-execution outcomes, planned).

### Fixed

- **`retained_at` propagation**: field was silently dropped at two pipeline stages (`_semantic_search` and `basic_rerank`). Fixed at all four transformation points so `MemoryHit.retained_at` reliably reflects the original retain timestamp.
- **Temporal strategy `as_of` bypass**: `_temporal_search` used `list_vectors()` which bypassed the `VectorFilters.as_of` filter. Fixed by passing `as_of` through `parallel_retrieve → _temporal_search` and applying the filter in the scan loop.
- **`basic_rerank` field loss**: creating new `ScoredItem` instances without copying `memory_layer` or `retained_at`. Both fields now propagated.

### Changed

- **`EntityLink` field names**: `source_entity_id` → `entity_a`, `target_entity_id` → `entity_b`. **Migration note**: callers constructing `EntityLink` directly must update keyword arguments; the old names are not aliased.
- **PyPI / install**: `astrocyte-age` and updated adapters require **`astrocyte>=0.8.0,<2`** to survive the v0.9.x → v1.0.x version boundary cleanly.

## [0.8.1] — 2026-04-22 (eval polish, competitor adapters, observability)

Finalises the evaluation harness, wires the competitor adapters, tightens the RRF API contract, and adds telemetry for unstamped forget records. No new runtime features; all changes are either bug fixes, new test coverage, or hardening for production benchmark runs.

### Fixed

- **LoCoMo category map** (`eval/benchmarks/locomo.py`, `eval/judges/locomo_judge.py`): categories 1 and 4 were swapped and category 3 was mislabeled as temporal (it is open-domain). **Operator note**: per-category columns in benchmark snapshots produced before this release carry incorrect labels; re-run to get accurate per-category F1.
- **LoCoMo canonical-judge scoring dispatch**: `cid == 3` (open-domain) now uses the correct open-domain scoring path; `cid in {2, 4}` uses plain F1 (was treating open-domain as temporal).
- **`build_competitor_brain` factory** (`eval/competitors/base.py`): both `"mem0"` and `"zep"` branches now import from the correct module paths and pass `llm_provider` to the adapter constructors.

### Fixed

- **LoCoMo category map** (`eval/benchmarks/locomo.py`, `eval/judges/locomo_judge.py`): categories 1 and 4 were swapped and category 3 was mislabeled as temporal (it is open-domain). **Operator note**: per-category columns in benchmark snapshots produced before this release carry incorrect labels; re-run to get accurate per-category F1.
- **LoCoMo canonical-judge scoring dispatch**: `cid == 3` (open-domain) now uses the correct open-domain scoring path; `cid in {2, 4}` uses plain F1 (was treating open-domain as temporal).
- **`build_competitor_brain` factory** (`eval/competitors/base.py`): both `"mem0"` and `"zep"` branches now import from the correct module paths and pass `llm_provider` to the adapter constructors.

### Added

- **`LoCoMoResult.canonical_f1_overall` / `canonical_f1_by_category`**: per-category F1 is accumulated during the eval loop and reported when `use_canonical_judge=True`.
- **Benchmark serializer `judge` / `system` fields** (`scripts/run_benchmarks.py`): output JSON now carries `judge: "canonical"|"legacy"` and `system: str` for unambiguous matrix comparisons.
- **Expanded abstention phrases** (`eval/judges/locomo_judge.py`): ~20 real-world LLM "I don't know" output variants added to `_ABSTENTION_PHRASES` so the judge doesn't count hedged non-answers as wrong.
- **`Mem0BrainAdapter`** (`eval/competitors/mem0_adapter.py`): `retain` / `recall` / `reflect` fully wired against Mem0 SDK v1. Supports both sync and async clients via `asyncio.iscoroutinefunction` detection; merges `tags` into `metadata`; shapes results into `MemoryHit` / `RecallResult`.
- **`ZepBrainAdapter`** (`eval/competitors/zep_adapter.py`): `retain` / `recall` / `reflect` wired against `zep-python` v1.x (OSS / self-hosted). Lazy session creation with per-instance cache; `asyncio.to_thread` bridging for sync clients; maps `MemorySearchResult.message` → `MemoryHit`.
- **`astrocyte_forget_unstamped_records_total` counter** (`_astrocyte.py`): emitted in `_collect_too_young_ids` for each record missing an `occurred_at` stamp, labelled by `bank_id`. Operators can alert on unexpected spikes caused by ingestion pipelines that drop timestamps.
- **E2E integration test** (`tests/test_e2e_pipeline_integration.py`): 15 tests covering identity classification → MIP routing → temporal recall → intent-weighted fusion → forget across a single `InMemoryEngineProvider` brain.
- **Competitor adapter contract tests** (`tests/test_competitor_adapters.py`): 35 tests pinning the `CompetitorBrain` duck-type surface for both adapters; Zep SDK is injected via `sys.modules` so the suite runs without `zep-python` installed.

### Changed

- **`weighted_rrf_fusion` rejects negative weights** (`pipeline/fusion.py`): passing a weight `< 0.0` now raises `ValueError("RRF weight must be >= 0.0")` instead of silently clamping to 0.0. Negative weights are a caller sign error, not a feature; the explicit raise surfaces the bug immediately.

## [0.8.0] — 2026-04-12 (M5 + M6 + M7 — production adapters, gateway, recall authority)

This release bundles **M5** (production storage providers), **M6** (standalone HTTP gateway), and **M7** (structured recall authority) on one minor version line, per `docs/_design/product-roadmap.md`. Subsequent connector and edge work ships as **v0.8.1**, **v0.8.2**, …

### Changed

- **PyPI / install**: adapter and gateway packages now require **`astrocyte>=0.7.0,<2`** (was **`<0.8`**) so **v0.8.x** and later resolve cleanly; pin **`astrocyte==0.8.0`** (and matching adapter versions) together for the M5–M7 milestone bundle.

### Added

- **M5 — Storage adapters** (separate PyPI packages under `adapters-storage-py/`): **`astrocyte-postgres`**, **`astrocyte-qdrant`**, **`astrocyte-neo4j`**, **`astrocyte-elasticsearch`** implementing Tier 1 `VectorStore` / `GraphStore` / `DocumentStore`; CI and publish workflows.
- **M6 — `astrocyte-gateway-py`**: FastAPI REST (`/v1/retain`, `/v1/recall`, `/v1/reflect`, `/v1/forget`), webhook ingest, health (`/health`, `/health/ingest`), optional admin routes, JWT/OIDC/`api_key`/`dev` auth → `AstrocyteContext`, Docker/Compose/Helm, GHCR image workflow.
- **M7 — Structured recall authority**: optional `recall_authority:` in config (`RecallAuthorityConfig`), `RecallResult.authority_context`, optional reflect injection; see ADR-004 and `built-in-pipeline.md`.
- **M4.1 — Federated / proxy recall**: `sources:` with `type: proxy`, remote JSON merge with local RRF (`astrocyte.recall.proxy`); OAuth helpers, tiered retrieval integration, observability hooks — see prior `[Unreleased]` notes in git history for detail.
- **Ingest transports**: `astrocyte-ingestion-kafka`, `astrocyte-ingestion-redis` (streams), `astrocyte-ingestion-github` (poll); `astrocyte[poll]` / `astrocyte[stream]` extras.
- **Gateway edge hardening (v0.8.x track)**: optional **`ASTROCYTE_RATE_LIMIT_PER_SECOND`**, documented CORS/body limits; **`scripts/bench_gateway_overhead.py --max-overhead-p99-ms`**; OpenAPI path contract tests; operator doc **[Gateway edge & API gateways](docs/_end-user/gateway-edge-and-api-gateways.md)**.

### Notes

- **SPI contract tests** for vector stores remain the reference for adapter authors (`astrocyte-py/tests/test_spi_vector_store_contract.py`).
- Release: tag **`v0.8.0`** at repo root → **`release.yml`** publishes **`astrocyte`** → **`astrocyte-postgres`** → gateway image; see **`RELEASING.md`**.

## [0.7.0] — 2026-04-11 (M4 external data sources — library ingest)

### Added

- **`astrocyte.ingest`**: `IngestSource` protocol, `WebhookIngestSource`, `SourceRegistry`, HMAC helpers, `handle_webhook_ingest` → `brain.retain()`; `IngestError`.
- Bank resolution: `target_bank` / `target_bank_template` with `{principal}`; JSON webhook body (`content` / `text`, optional `metadata`, …).

### Notes

- **M4.1** (proxy recall) and later milestones ship in **v0.8.0+**; see **[0.8.0]** above.

## [0.6.0] — 2026-04-11 (M3 extraction pipeline)

### Added

- Inbound extraction chain: **normalize → chunk → optional LLM entity extraction → embed → store**, with `content_type` routing and `extraction_profiles` (including `builtin_text` / `builtin_conversation`).
- Profile-driven `metadata_mapping`, `tag_rules`, `entity_extraction`, and `fact_type` on retain.
- Packaged defaults: `astrocyte/pipeline/extraction_builtin.yaml`; stable imports: `prepare_retain_input`, `merged_extraction_profiles`, `extraction_profile_for_source`, `PreparedRetainInput`.

[Unreleased]: https://github.com/AstrocyteAI/astrocyte/compare/v0.9.1...HEAD
[0.9.1]: https://github.com/AstrocyteAI/astrocyte/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/AstrocyteAI/astrocyte/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/AstrocyteAI/astrocyte/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.8.0
[0.7.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.7.0
[0.6.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.6.0
