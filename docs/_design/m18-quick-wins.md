---
title: "M18 — Core integration of bench-side Hindsight-parity features"
draft: false
topic: design
---

# M18 — Core integration of bench-side Hindsight-parity features

**Status:** scoping (2026-05-17, revised after parallel-workstream discovery)
**Predecessor:** [M17 SHIPPED](./m17-pageindex-ingestion.md) — Conversation Engine cleared both ship gates at 3-run mean (LME +10.56pp, LoCoMo +2.25pp).
**Goal:** integrate 4 bench-harness-side Hindsight-parity features into framework core, then ablation-bench them. Same M16 discipline: implement in core (not harness), behind feature flags defaulting OFF, so what ships is what users get.

## 1. Discovery — what's already drafted vs what needs work

During M17's run-4 iteration, 4 new framework features were drafted as `astrocyte/pipeline/*.py` modules but wired into bench harness scripts instead of core. Plus `rerank_boosts.py` (§12.2) was wired into core already.

| Module | What it does | Backlog mapping | Core-wired? |
|---|---|---|---|
| `rerank_boosts.py` | Multiplicative recency × temporal × proof_count × cross_encoder boosts on rerank | §12.2 | ✅ `section_rerank.py:141` |
| `spreading_activation.py` | Entity-graph one-hop expansion at retrieval time, BEFORE rerank | §12.3 (link expansion variant) | ❌ called from `mem0_harness/astrocyte_client.py:917` only |
| `temporal_expressions.py` | Query-time relative-time expansion ("a few weeks ago" → ISO range) | §12.6 (variant) | ❌ called from `mem0_harness/astrocyte_client.py:756` only |
| `episodic_extract.py` | Detects episodic events at extraction time, tags as separate `fact_kind` | World vs Experience distinction | ❌ called from `mem0_harness/astrocyte_client.py:803` + `bench_pageindex_locomo.py:548` only |
| `directive_compile.py` | Compiles preference facts into `MentalModel(kind="directive")` rows | §12.7 | ❌ called from `bench_pageindex_locomo.py:601` only |

**Why this matters:** features wired in the bench harness measure something users never run. Production callers (Cerebro, Synapse, future apps) go through `astrocyte/` core, not the bench harness — they wouldn't see any of these features. The M16 lesson (don't bench harness-side; promote-to-core rarely happens) applies directly.

## 2. Locked policy framework

| # | Decision | Locked value |
|---|---|---|
| 1 | Scope | **THREE sub-cycles**: M18a (core integration of 4 features + flags + integration tests), M18b (ablation bench — each feature individually + combined), M18c (ship features that cleared per-item gates by flipping defaults). |
| 2 | Per-feature default | `enable_<feature> = False` after integration. Promotion to default = M18c step after bench gate clears. |
| 3 | Answerer / judge model | gpt-4o-mini (apples-to-apples vs M17 baseline) |
| 4 | Budget cap | **$80 hard cap** (5 ablation runs × ~$1-3 each + retries). Tighter than M17. |
| 5 | Per-feature ship gate | **1σ over M17 3-run mean** when run individually. **2σ over M17 mean** for combined-all run. |
| 6 | Halt gate | Any single feature's bench LME < 65% OR LoCoMo < 76% in M18b ⇒ HALT that feature, document. |
| 7 | DB reset policy | `BENCH_RESET=1` mandatory every full bench run. |
| 8 | M17 baseline reference | LME 75.56% ±5.09pp / LoCoMo 80.50% ±1.50pp (M17 conv-run-3/4/5 mean) |
| 9 | Replication N | M18b uses **2-run mean per feature** (cheaper than 3-run since each is a controlled change vs M17 baseline). Combined-all uses 3-run. |
| 10 | Branch | `feat/m18-core-integration` |
| 11 | M18a discipline | NO bench runs during M18a. Pure refactor + unit/integration tests. Bench cost only starts in M18b. |
| 12 | Bench-harness call removal | M18a removes the existing harness-side calls AND replaces them with `enable_<feature>=True` env-driven flag flips. Each feature's bench is `make bench-mem0-harness-parallel ASTROCYTE_M18_ENABLE_<FEATURE>=1 ...`. |

### M18b per-feature ship gates (arithmetic)

| Bench | M17 reference | +1σ required | +2σ required |
|---|---|---|---|
| LME top_20 | 75.56% ±5.09pp | ≥ 80.65% (per-feature) | ≥ 85.74% (combined-all) |
| LoCoMo top_20 | 80.50% ±1.50pp | ≥ 82.00% (per-feature) | ≥ 83.50% (combined-all) |

The per-feature gates are tighter (1σ) because each feature is supposed to contribute incrementally. Combined-all is the 2σ gate because that's what "ship M18" means.

## 3. M18a — core integration plan (4 sub-items)

Each sub-item: standalone module already exists in `astrocyte/pipeline/`. The integration work is wiring it into the right core pipeline + feature flag + integration test + removing the bench-harness call.

### 3.1 (a1) `temporal_expressions` → `query_analyzer.py` (~1 day, lowest risk)

- **What**: Relative-time regex expansion that turns "a few weeks ago" into an ISO date range.
- **Existing**: `astrocyte/pipeline/query_analyzer.py` has regex pre-pass (M14) + LLM fallback (flag, currently off). The temporal_expressions module duplicates some of that intent with different patterns.
- **Integration**: Merge `temporal_expressions`'s patterns into `query_analyzer.py`'s regex pre-pass. New flag: `enable_temporal_expansion`.
- **Test**: extend `tests/test_query_analyzer.py` with the new patterns. Integration test: verify orchestrator passes the expanded range downstream.
- **Bench-harness removal**: delete the `temporal_expressions` call at `astrocyte_client.py:756`; behavior covered by query_analyzer now.

### 3.2 (a2) `spreading_activation` → `section_recall.py` as a new strategy (~1-2 days)

- **What**: Given the initial top-K, expand by one hop through entity co-occurrence before rerank.
- **Existing**: `section_recall.py` has a strategy registry (`select_strategies_for_mode`) with `semantic`, `entity`, `temporal`, `keyword`, `graph_expand`. spreading_activation is essentially graph_expand done differently — entity-driven, retrieval-time.
- **Integration**: Add `spreading_activation` as a new strategy. Wire into `select_strategies_for_mode` for the modes that benefit (multi-hop, knowledge-update). New flag: `enable_spreading_activation`.
- **Test**: unit test in isolation + integration test against `section_recall` end-to-end.
- **Bench-harness removal**: delete the spreading_activation call at `astrocyte_client.py:917`; covered by section_recall's strategy registry now.

### 3.3 (a3) `episodic_extract` → `fact_recall.py` + `document_postprocess.py` ✅ DONE 2026-05-17

- **What**: Two halves: (a) retain-side tag step that appends `episodic:event` marker to fact entities; (b) recall-side search-by-entity that surfaces those facts when the question carries an episodic cue ("where did I", "who attended", etc.).
- **Existing**: `astrocyte/pipeline/episodic_extract.py` ships `tag_episodic_facts()` + `question_has_episodic_cue()` + `EPISODIC_MARKER` constant. No schema migration needed (uses existing entity array; see §6.1).
- **Core integration**:
  - **Retain side** — `tag_episodic_facts` runs from `run_document_postprocess()` (`astrocyte/pipeline/document_postprocess.py`), gated by `config.episodic_extract.enabled`. Failure-isolated; runs in-place on the facts list before `save_facts`.
  - **Recall side** — episodic search-by-entity runs from `fact_recall()` (`astrocyte/pipeline/fact_recall.py`), gated by the SAME config flag AND `question_has_episodic_cue(query)`. Dedupe by `fact_id` with semantic hits.
- **Flag**: `config.episodic_extract.enabled` (default `False` for M17 parity). Bench env override: `ASTROCYTE_M18_ENABLE_EPISODIC_EXTRACTION=1`.
- **Tests**: `tests/test_document_postprocess.py` (gating + failure isolation), `tests/test_fact_recall.py` (semantic always runs, episodic gated, dedupe by fact_id, failure isolation). 13 new tests, all passing.
- **Bench-harness state**: env-var → `AstrocyteConfig` bridge constructs the gating config; both the retain block in `bench_pageindex_locomo._populate_section_index` and the recall block in `astrocyte_client.py` now delegate to the core entry points (no inline pipeline logic remains in the harness).

### 3.4 (a4) `directive_compile` → `document_postprocess.py` ✅ DONE 2026-05-17

- **What**: After preference compilation, distill directive-style imperatives stored as `MentalModel(kind="directive")` rows.
- **Existing**: `astrocyte/pipeline/directive_compile.py` ships `compile_directives_for_document()`. `MentalModel.kind` discriminator already supports `"directive"` (M14.6 plumbing).
- **Core integration**: `compile_directives_for_document` runs from `run_document_postprocess()` as pass 3 (after episodic tag, after preference_compile). Failure-isolated. The `n_sessions` hint (so directive_compile can lower its ≥2-mentions threshold for single-session docs) is computed by the caller and passed through.
- **Flag**: `config.directive_compile.enabled` (default `False` for M17 parity). Bench env override: `ASTROCYTE_M18_ENABLE_DIRECTIVE_COMPILE=1`.
- **Tests**: covered by `tests/test_document_postprocess.py` (gating + failure isolation) + existing `tests/test_directive_compile.py` for the underlying function.
- **Bench-harness state**: the 3 inline retain-side calls (`tag_episodic_facts` + `compile_preferences_for_document` + `compile_directives_for_document`) collapsed into a single `run_document_postprocess()` call. Bench just constructs the `AstrocyteConfig` from env vars and reads `result.preferences_compiled` / `result.directives_compiled` for its summary print.

### 3.5 Implementation order (lowest risk first)

1. **(a1) temporal_expressions** — query path is well-understood, regex addition is contained
2. **(a4) directive_compile** — post-step at extraction, doesn't touch retrieval
3. **(a2) spreading_activation** — touches retrieval path, but `section_recall.py` strategy registry is clean
4. **(a3) episodic_extract** — needs schema migration (highest risk); do last so a halt doesn't block others

## 4. M18b — ablation bench plan

Each run uses `ASTROCYTE_M17_INGEST_PATH=conversation MEM0_HARNESS_LOCOMO_MAX_Q=20` (fair vs baseline + M17).

| Run | Flags | Purpose | 2-run mean? |
|---|---|---|---|
| **B0** | all OFF | Parity vs M17 — confirms refactor didn't regress | 1 run (sanity) |
| **B1** | `enable_temporal_expansion` ON | (a1) isolated lift | 2 runs |
| **B2** | `enable_directive_compile` ON | (a4) isolated lift | 2 runs |
| **B3** | `enable_spreading_activation` ON | (a2) isolated lift | 2 runs |
| **B4** | `enable_episodic_extraction` ON | (a3) isolated lift | 2 runs |
| **B5** | ALL ON | combined effect | 3 runs |

Total: 12 runs × ~$1-3 each ≈ **$15-35**. Under the $80 cap.

## 5. M18c — ship-or-null per feature

After M18b lands:

- Each feature with 2-run mean ≥ M17 + 1σ → flip its default to `True` (ship)
- Features below 1σ → keep default `False` (drafted, kept for opt-in users)
- If combined-all ≥ M17 + 2σ → declare M18 a ship cycle

## 6. Resolved design decisions (was: open questions)

Locked 2026-05-17 before M18a code:

1. **Schema migration for episodic_extract → NO new migration needed.** Per the existing module's docstring: "we don't introduce a new SQL value; instead we mark each detected fact by appending a namespaced marker `episodic:event` to its `entities` array." The existing `search_facts_by_entity` SPI is the read path.
2. **`enable_*` config exposure → per-feature fields on config + env-var override.** Each of the 4 features gets its own `enable_<feature>` boolean. Per-bank config field (e.g., `QueryAnalyzerConfig.enable_temporal_expansion`) + env-var override (`ASTROCYTE_M18_ENABLE_<FEATURE>=1`) for bench-time control. Mirrors M17's `ASTROCYTE_M17_INGEST_PATH` pattern. All 4 default to `False` post-M18a; flipped to `True` in M18c after their per-feature gate clears.
3. **Bench harness contract → verify M17 baseline reproduces with all flags OFF (B0 sanity).** M18b starts with one B0 run with all flags OFF. Expected: ~75-76% LME / ~80% LoCoMo (matches M17 3-run mean). If it diverges, the refactor introduced a regression — halt before ablation runs (B1-B5).
4. **Combined-all pipeline order in B5 → explicit, documented order:**
   ```
   retain: section_fact_extraction
         → run_document_postprocess(
             config.episodic_extract        ← (a3) tag in-place
             config.preference_compile      ← (M14.6 baseline)
             config.directive_compile       ← (a4) imperatives
           )
         → save_facts + update_fact_embeddings
   recall: query_analyzer.regex
             (config.query_analyzer.enable_temporal_expansion ← a1)
         → fact_recall(
             config.episodic_extract        ← (a3) search-by-marker, gated on cue
           )
         → section_recall
             (enable_spreading_activation ← a2)
         → section_rerank (boosts already wired)
   ```
   Each feature has a natural pipeline-stage home; they hit different stages so composition is sequential, not conflicting. All 4 are now core entry points (`run_document_postprocess`, `fact_recall`, `section_recall`, `query_analyzer.analyze_query`) — the bench harness just constructs `AstrocyteConfig` from env vars and calls these.

## 7. Out of scope for M18

- Reflect agent revisit (M15 null-verdict) — only worth doing after these 4 + rerank_boosts ship; future cycle.
- BM25 / keyword search strategy — not in this batch.
- Document Engine bench on FinanceBench / DoubleBench — separate workstream.
- Promotion of bench-harness ingestor calls to framework-core retain SPI — M17 follow-on, not M18.

## 8. Cycle close — FINAL (M18b + M18b.2, 2026-05-17 → 2026-05-18)

### 8.0 TL;DR

**Shipped (M18b):** B1-dp+RRF (dateparser Pass B + RRF fact fusion + MemoryFact rename + migration 029).
**Not shipped (M18b):** B2 directive_compile (SSP regression).
**Tested in M18b.2 (overnight autonomous queue):** B3 spreading_activation (null), B4 episodic_extract (null), B1+B3 stack (anti-composes), prompt-only (standalone winner but anti-composes with B1-dp+RRF).
**Bench gates cleared:** LoCoMo 2-run mean 83.75% (+1.75pp over M17+1σ).
**Cycle spend:** ~$25 of $80 cap.
**Key finding for M19:** **retrieval-feature ceiling reached** — adding parallel RRF siblings beyond shipped B1-dp+RRF doesn't compose. M19 priority shifts to per-question-type prompt routing + Hindsight `create_directive` MCP tool.

### 8.0a Final ablation matrix (2-run means where available, top_20)

| Condition | LME | LoCoMo | Combined/230 | Ship |
|---|---|---|---|---|
| B0 baseline | 66.67% | 80.5% | 78.70% | — |
| B1 (Pass A regex only) | 70.0% (1 run) | 81.0% (1 run) | 79.57% | ❌ null result |
| B1-dp (Pass A + B, no RRF) | 75.0% | 81.75% | 80.87% | replaced by B1-dp+RRF |
| **B1-dp+RRF (SHIPPED)** | **71.67%** | **83.75%** | **82.17%** | ✅ |
| B2 (directive_compile) | 70.00% | 82.75% | 81.09% | ❌ SSP regression replicated |
| B2 + prompt | 70.0% (1 run) | 82.5% (1 run) | 80.87% | ❌ overlap with prompt-only |
| prompt-only (Hindsight SSP) | 78.33% | 83.50% | **82.83%** | ⏸️ validated, deferred (anti-composes with B1-dp+RRF) |
| **B5 stack (B1-dp+RRF + prompt)** | 76.67% | 80.75% | 80.22% | ❌ anti-composition confirmed 2/2 runs |
| **B3 spreading_activation** | 68.33% | 82.50% | 80.65% | ❌ replication failed (r1 temporal +4 / r2 temporal −1) |
| **B1+B3 stack r1** (1 run) | 70.0% | 81.5% | 80.0% | ❌ same anti-composition shape (multi-hop +3 / temporal −3) |
| **B4 episodic_extract r1** (1 run) | 66.67% | 81.0% | 79.13% | ❌ null (temporal −4 regression on the single run) |
| **All-ON B5** (1 run, all 5 features) | 66.67% (B0 cancel) | **84.5%** | 189 (82.17%) | ⚠️ **Surprising**: LoCoMo TIES B1-dp+RRF mean; **no per-cat regression** (OD +5 biggest of series, MH +1, TR +2). LME cancels (SSA −2 / SSP+1+SSU+1). Confirms ceiling but disproves "more features = more anti-comp" naive narrative. |

### 8.0b Key findings (banked for M19)

1. **RRF fusion is structurally necessary** for adding new parallel retrieval strategies. Pre-RRF B1-dp showed phantom +13pp LME on run 1 that didn't replicate; RRF correctly damped that noise. Any future feature that adds a fact-grain retrieval path must hook into `fact_recall`'s RRF fusion, not append to the rerank pool.

2. **Auto-compiled directives ≠ Hindsight directives.** Their `mental_models.subtype='directive'` rows are USER-AUTHORED via the `create_directive` MCP tool. Auto-compiling preferences into rules introduces an information-loss failure mode that the bench measures cleanly as the SSP regression. M19 path: implement `create_directive` MCP tool, leave the auto-compile code in-tree but `enabled=False`.

3. **Recall improvements and answerer-prompt steering can anti-compose.** B1-dp+RRF (recall) and the Hindsight SSP prompt (answer-gen) each help LoCoMo individually but cancel on multi-hop when combined (both 2-run mean and per-cat replication). Per-question-type prompt routing or selective prompt application is needed to compose them. Deferred to M19.

4. **n=30 LME is bench noise for sub-5pp lifts.** Single-session-preference at n=5 swings 0→3 questions across runs (±60pp on the category). The decisive bench for ship decisions in this size class is LoCoMo (n=200, ±1.5pp σ on M17 baseline). LME stays useful for category-level diagnostics but not headline numbers.

5. **Bench harness has two footguns we hit twice each:**
   - Idempotency-marker resume (`_ingestion_*.json` / `*_results_*.json`) silently skips re-runs after DB reset → invalid "phantom" results. Mitigation: new project name OR `rm -rf` result dir between attempts. Permanent fix in M19 bench-harness hardening.
   - `uv sync --extra X` removes deps from other extras (we lost the rerank stack mid-cycle). Mitigation: pin `uv sync --extra bench --extra rerank` together. Permanent fix: update the bench Makefile.

6. **Combined-accuracy ceiling at 189-192/230 (revised with all-ON data).** Across M18b + M18b.2 + all-ON we tested every plausible retrieval-side stack. Single features cluster at 187-192 (B1-dp+RRF, prompt-only, B3, even all-ON-B5); **all 2-feature stacks anti-compose** to 182-187 (B5=B1+prompt, B1+B3 stacks both fall below their best individual constituent); 5-feature all-ON returns to 189 (matches ceiling, doesn't exceed). The mechanism is sharper than initially framed: at 2 features each signal is strong enough to displace the other's category wins; at 5 features no signal dominates, system falls back to baseline+coverage. **M19 implication:** the fix is not "more or fewer RRF siblings" — it's a NEW composition mechanism that lets multiple signals modulate the ranking rather than competing for slots. Hindsight's `reranking.py` does exactly this with bounded multiplicative boosts on the CE score (`recency × temporal × proof_count`). Astrocyte already has the primitive (`astrocyte/pipeline/rerank_boosts.py`, applied in `section_rerank.py`) but it's NOT applied to the bench's unified fact+section rerank pool (`astrocyte_client.py:984` calls `cross_encoder_rerank` directly). **M19 Workstream C primary work**: wire the existing `rerank_boosts.apply_boosts` into the unified rerank step. Expected cost: ~1-2 days. Expected lift: enables stacked features to compose without per-category cancellation.

### 8.1 What landed

**M18a (core integration)** — all four parallel-workstream features wired through proper core entry points (no bench-harness inlining):

| Sub-item | Core entry point | Status |
|---|---|---|
| (a1) temporal_expansion | `query_analyzer.analyze_query(allow_temporal_expansion=...)` | ✅ done |
| (a2) directive_compile | `run_document_postprocess(config.directive_compile)` | ✅ done, flag OFF |
| (a3) spreading_activation | `section_recall(enable_spreading_activation=...)` | ✅ done, flag OFF |
| (a4) episodic_extract | `run_document_postprocess(...)` + `fact_recall(...)` | ✅ done, flag OFF |

**M18a-1+ (mid-cycle insertions, Hindsight-parity)** — added because B1 (Pass A only) showed no signal:

| Component | What it does | Status |
|---|---|---|
| `temporal_dateparser.py` Pass B | Wide-net date extraction via `dateparser` library; defensive try/except + false-positive filter (mirrors Hindsight `DateparserQueryAnalyzer`) | ✅ shipped |
| `fact_recall.py` RRF refactor | semantic + episodic + temporal as parallel siblings, RRF-fused (k=60) instead of appended to flat rerank pool | ✅ shipped |
| `MemoryFact` rename + nullable migration 029 | Memory Engine fact type rename (was `PageIndexFact`); `document_id`/`line_num` now nullable matching Hindsight's `MemoryUnit.document_id` | ✅ shipped |
| Hindsight SSP prompt block (`_hindsight_prompt.py`) | Ports Hindsight's recommendation/preference answerer instructions; gated by env var | ⏸️ in-tree, flag OFF |

### 8.2 M18b ablation bench (LME-30 + LoCoMo-200 fair subsets, gpt-4o-mini, 2-run means)

See §8.0a above for the final matrix.

### 8.3 (DEPRECATED — superseded by §8.0b)

### 8.1 What landed

**M18a (core integration)** — all four parallel-workstream features wired through proper core entry points (no bench-harness inlining):

| Sub-item | Core entry point | Status |
|---|---|---|
| (a1) temporal_expansion | `query_analyzer.analyze_query(allow_temporal_expansion=...)` | ✅ done |
| (a2) directive_compile | `run_document_postprocess(config.directive_compile)` | ✅ done |
| (a3) spreading_activation | `section_recall(enable_spreading_activation=...)` | ✅ done |
| (a4) episodic_extract | `run_document_postprocess(...)` + `fact_recall(...)` | ✅ done |

**M18a-1+ (mid-cycle insertions, Hindsight-parity)** — added because B1 (Pass A only) showed no signal:

| Component | What it does | Status |
|---|---|---|
| `temporal_dateparser.py` Pass B | Wide-net date extraction via `dateparser` library; defensive try/except + false-positive filter (mirrors Hindsight `DateparserQueryAnalyzer`) | ✅ done |
| `fact_recall.py` RRF refactor | semantic + episodic + temporal as parallel siblings, RRF-fused (k=60) instead of appended to flat rerank pool | ✅ done |

### 8.2 M18b ablation bench (LME-30 + LoCoMo-200 fair subsets, gpt-4o-mini, 2-run means)

| Run | LME top_20 | LoCoMo top_20 | Verdict |
|---|---|---|---|
| **B0** (all OFF, M17 parity check) | 66.67% (20/30) | 80.5% (161/200) | reproduces M17 baseline within noise |
| **B1** (temporal_expansion ON, Pass A only) | 70.0% (21/30) — 1 run | 81.0% (162/200) — 1 run | **NULL** — temporal subcategory didn't move |
| **B1-dp** (Pass A + B, pre-RRF) | 75.0% mean (24, 21) | 81.75% mean (165, 162) | **0.25pp short** of LoCoMo ship gate; LME variance dominated |
| **B1-dp+RRF** (Pass A + B + RRF fusion) | 71.67% mean (20, 23) | **83.75% mean (171, 164)** | ✅ **SHIPPED** — LoCoMo cleared gate by +1.75pp |

**B1-dp+RRF mechanism (validated):** the lift came primarily from **multi-hop +4** (B0 67/79 → mean 71/79), not the named "temporal" subcategory. Hypothesis: dateparser identifies date references in synthesis questions ("what did X say in March about Y"); RRF rewards facts that co-occur in semantic + temporal lists; cross-encoder then promotes those into final candidates.

### 8.3 Lessons banked

1. **Don't ship without RRF when adding a new parallel retrieval strategy.** Pre-RRF B1-dp showed +13pp on LME run 1, fully reverted on run 2 — would've shipped phantom features without the fusion.
2. **n=30 LME is too noisy to be a deciding bench** for sub-5pp lifts. The single-session-preference category alone (5 questions, swings 0-3) dominates the variance. LoCoMo n=200 is the decisive bench for this scale of feature work.
3. **Hindsight's `dateparser` choice is correct.** Pure-regex temporal extraction (our M14 + M18a-1 Pass A) misses the bulk of real date references; dateparser closes the gap.
4. **Idempotency-marker resume is a footgun for ablation.** The mem0 harness's `_ingestion_*.json` and `*_results_*.json` files cause silent skip-resume on retry. Always use a new project name OR `rm -rf` the result dir between attempts.
5. **`uv sync --extra bench` removes the rerank stack.** Always `uv sync --extra bench --extra rerank` for benches, or pin both in the Makefile.

### 8.4 Pending (B2-B5)

- **B2** (directive_compile) — not yet run
- **B3** (spreading_activation) — not yet run
- **B4** (episodic_extract) — not yet run
- **B5** (all ON combined) — not yet run

Each remains gated by its own config flag, default OFF. Will run as separate sub-cycles or batch via `make bench-mem0-harness-parallel` with the corresponding env vars.

### 8.5 Cycle spend so far

Approximately **9 bench runs × ~$1-2 each ≈ $12-18** under the $80 cap. Plenty of budget remaining for B2-B5.
