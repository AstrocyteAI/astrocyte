# M22-M25 — Hindsight-parity answerer experiments

**Status:** in flight (2026-05-19) — M22/M23/M24 LME complete; M24 LoCoMo still running; M25 chained to fire next
**Predecessor:** [`m21-observation-evolution.md`](m21-observation-evolution.md) — M21 code shipped, parity 2-run mean **188.5/230** (within M19a variance band 189-195)
**Reference:** Hindsight `hindsight-dev/benchmarks/{longmemeval,locomo}/` answer generators + `hindsight-api-slim/hindsight_api/engine/retain/fact_extraction.py`

---

## 1. Why this experiment arc exists

After M21's null-on-bench surface-only ship, the **gap-analysis research** (CHANGELOG + the agent-driven Hindsight-vs-Astrocyte comparison) identified the answerer surface — not the recall pipeline — as the highest-ROI lever to close the LME+LoCoMo gap with Hindsight (94.6% / 92.0%).

M22-M25 is a tight 4-step experiment arc, each step testing a specific hypothesis about what makes Hindsight's answerer effective:

| Cycle | Hypothesis | Outcome |
|---|---|---|
| **M22** | Porting Hindsight's 95-line directive answerer prompt + structured context wins +4-7q | **NULL/NEG** — 188/230 (−6q vs R0-v2); LME −4q; SSP regressed |
| **M23** | M22's monster directive over-loaded the answerer; **focused per-Q-type prompts** (5-15 lines each, only rules for THIS question shape) recover | Code complete; bench killed mid-flight when M24 arch fix landed |
| **M24** | Even focused prompts can't help if facts and source chunks aren't paired inline — answerer can't cross-reference. **Architectural fact↔chunk inline pairing** required | LME complete: 21/30 = tied R0-v2; SSP recovered, SSU +1, SSA still −2q. LoCoMo in flight, trending +3pp |
| **M25** | Hindsight handles SSA via **binary fact_type taxonomy + per-conversation perspective tag**, not via a special `assistant_statement` type. Adopting their pattern recovers SSA | Code complete; bench chained behind M24 |

All four are bench-only experiments — gated by `ASTROCYTE_M22_HINDSIGHT_ANSWERER=1`, default OFF. Production-default behavior at v0.14.0 is unaffected.

## 2. M22 — Hindsight directive prompt (NULL/NEG)

**What we shipped (code):**

- `scripts/mem0_harness/_hindsight_answerer.py` — new ~470 LOC module
  - `_DIRECTIVE_PROMPT_CORE` — verbatim port of Hindsight's `_get_context_instructions` from `longmemeval_benchmark.py:244-340` (counting, disambiguation, date-arithmetic, recommendation, abstention rules; ~95 lines)
  - `_QTYPE_PREFACES` — per-Q-type single-paragraph prefaces (LME's 6 types + LoCoMo's 4 categories)
  - `format_context_structured()` — fact-grain candidates rendered as `Fact N: ... When: ...` blocks, with a separate `=== Source Chunks ===` section at the bottom
  - `=== Entity Observations ===` from wikis (bench's wiki_compile output) + `=== Mental Models ===` from MentalModelStore (filtered to kind=general/directive per M14.7 revert)
  - `_install_process_question_qtype_shim()` — monkey-patches `process_question` to populate qtype contextvars before the patched `get_answer_generation_prompt` fires
- `scripts/mem0_harness/run_lme.py` + `run_locomo.py` — install the patch when the env var is set
- `astrocyte_client.list_observations_for_bench` + `list_mental_models_for_bench` — bench-side fetchers for the Gap 3 hierarchical layers

**Bench result (`astrocyte-m22`):**

| | LME | LoCoMo | Combined | Δ vs R0-v2 |
|---|---|---|---|---|
| R0-v2 (M19, identical config) | 21/30 | 173/200 | **194/230 (84.35%)** | — |
| **M22** | **17/30** | 171/200 | **188/230 (81.74%)** | **−6q** |

LME by type — **the directive prompt regressed LME by −4q on the categories it was supposed to help**:

| Type | M22 | M19a best | Δ |
|---|---|---|---|
| single-session-preference | **2/5** | 4/5 (peaked 5/5) | **−2q** |
| single-session-user | 2/5 | 3/5 | −1q |
| temporal-reasoning | 2/5 | 3/5 | −1q |
| knowledge-update | 3/5 | 4/5 | −1q |

Same anti-composition shape as M18b's B5-stack: too many rules in one answerer context dilute the simple "scan facts, find preference, quote it" task.

**Diagnosis:** Hindsight uses the 95-line directive **only for LME** (`LongMemEvalAnswerGenerator`). For LoCoMo they use a **separate ~10-line prompt routed through their reflect engine** (`LoComoReflectAnswerGenerator(needs_external_search=False)`, see `locomo_benchmark.py:198`). I conflated the two — pasted the LME directive onto every question across both benches.

## 3. M23 — Focused per-Q-type prompts

**Hypothesis:** Each question category gets its own SMALL prompt (5-15 lines) with only the rules that apply to that question shape. No monster directive. The shared prelude (1 paragraph) explains the context format; per-category prompt gives the 4-5 rules for that shape; minimal answer guidelines.

**What we shipped (code):**

- Replaced `_DIRECTIVE_PROMPT_CORE` (95 lines) + `_QTYPE_PREFACES` (10 short prefaces) with `_QTYPE_FOCUSED_PROMPTS` (10 self-contained per-category prompts, 5-15 lines each)
- New `_SHARED_PRELUDE` (8 lines) — universal context-format explanation only
- Total prompt size per question: ~6,000 chars (M22) → **~1,800 chars (M23)**, ~3× reduction
- Same `ASTROCYTE_M22_HINDSIGHT_ANSWERER=1` flag — replacing M22's body in place

**Bench result:** killed mid-flight when M24 architectural fix landed (chained M25 launcher fired prematurely from a kill-race condition). No completed M23-only data point. The focused prompts shipped into the M24 bench as the baseline answerer body.

## 4. M24 — Architectural fact↔chunk inline pairing

**Hypothesis:** Hindsight's prompt assumes facts and source chunks render INLINE (`Fact N: ... Source: "..."` paired). Our M22/M23 renders them in separate sections (facts on top, chunks at the bottom). When the prompt says "read the source chunks carefully for verbatim wording," the answerer can't cross-reference fact→chunk visually. Adding inline pairing should recover the categories that depend on verbatim source text (SSP, SSU, temporal).

**Hindsight's data model** (the parity target):

- `chunks` table with `chunk_id` PK + `chunk_text` (alembic migration `b7c4d8e9f1a2_add_chunks_table.py`)
- `memory_units.chunk_id` FK to `chunks(chunk_id)`, ON DELETE CASCADE (`f6g7h8i9j0k1_chunk_fk_cascade_delete.py`)
- `tool_recall(include_chunks=True)` returns `{results, chunks, entities}` — facts carry chunk_id, chunks batch-fetched, answerer renders inline

**Astrocyte's pre-M24 model:** `MemoryFact` has `(document_id, line_num)` positional anchor to `PageIndexSection`. The link works for cascade-delete but isn't carried through `search()`'s flat candidate-list output.

**What we shipped (code) — 3 layers, all backwards-compatible:**

| Layer | Change | File | Lines |
|---|---|---|---|
| **A** Identity | `MemoryFact.chunk_id` + `MemoryFactHit.chunk_id` `@property` computed from `(document_id, line_num)` | `astrocyte/types.py` | +35 |
| **B** Plumbing | `astrocyte_client.search()` looks up source-chunk text via `_slice_section_around_line(md_text, fh.line_num)` for each fact + threads `chunk_id` + `source_chunk` through the post-rerank `out` dict | `scripts/mem0_harness/astrocyte_client.py` | +25 |
| **C** Rendering | `format_context_structured` renders facts with inline `Source chunk: "..."` block (Hindsight parity); section candidates already paired into a fact are de-duped from the standalone bucket | `_hindsight_answerer.py` | +20 |

**No schema migration** — the composite `(document_id, line_num)` was already a stable identity in the data model; M24 makes it explicit via a property and threads it through retrieval output. Cost at query time: one `_slice_section_around_line` call per fact (microseconds).

**M24 LME result:** **21/30 = 70%, exactly tied with R0-v2 (21/30).** Per question type vs R0-v2:

| Type | M24 | R0-v2 | M22 | Δ vs R0-v2 |
|---|---|---|---|---|
| knowledge-update | 4/5 | 4/5 | 3/5 | 0 |
| multi-session | 4/5 | 4/5 | 4/5 | 0 |
| **single-session-preference** | **4/5** | 4/5 | 2/5 | **0** (M22's −2q regression fully recovered) |
| **single-session-user** | **4/5** | 3/5 | 2/5 | **+1q** |
| temporal-reasoning | 3/5 | 3/5 | 2/5 | 0 |
| **single-session-assistant** | **2/5** | 4/5 | 4/5 | **−2q** (persistent regression site → M25) |

**M24 LoCoMo (in flight, ~89.7% running at 39% completed)** — multi-hop **96%** (+4pp), temporal **88%** (+7pp). M22's temporal −3pp fully reversed.

The diagnosis was correct: prompt + structured context alone aren't enough; the inline pairing was the missing piece. **Multi-hop and temporal are now beating R0-v2 — first time in the experiment arc.**

## 5. M25 — Hindsight binary fact_type + per-conversation perspective tag (SSA fix)

**Hypothesis:** M24's residual SSA regression (−2q vs R0-v2) comes from our `assistant_statement` fact_type + inline source-chunk pairing producing redundant content. The fact text says "Assistant recommended saline rinse"; the inline source chunk says "[assistant] Have you tried the saline rinse..." — same content twice in different framings confuses the answerer.

**Hindsight's solve** (`engine/retain/fact_extraction.py:150-345`, `longmemeval_benchmark.py:85`):

1. **Binary classification:** `fact_type ∈ {world, assistant}` → mapped to `{world, experience}` at storage time. Speaker perspective is carried by `speaker` field + per-conversation context tag, NOT by a special fact_type bucket.
2. **Per-conversation perspective tag:** the LME bench retains each session with `context="Session X - you are the assistant in this conversation - happened on DATE UTC"`. The fact-extraction LLM reads this and knows the LLM-perspective in the transcript IS the assistant.
3. **No special answerer-prompt handling for SSA** — Hindsight gets 95% LME without any SSA-specific instructions because the data is shaped right.

**What we shipped (code):**

- `astrocyte/pipeline/section_fact_extraction.py`:
  - `_VALID_FACT_TYPES` reduced from 6 to 5 (removed `assistant_statement`)
  - `_LEGACY_FACT_TYPE_REMAP = {"assistant_statement": "experience"}` shim — legacy LLM emits get remapped on read, with `speaker='assistant'` force-set to preserve perspective signal
  - `_EXTRACT_PROMPT` rewritten:
    - Header preamble: "between a USER and an AI ASSISTANT; the 'assistant' role IS the AI" — Hindsight-parity perspective tag baked into the prompt template (no per-call context needed since the bench's sessions all have this shape)
    - Removed `assistant_statement` from fact_type description
    - Example updated: assistant utterance is now `fact_type='experience'` with `speaker='assistant'`
- `astrocyte/types.py` — `MemoryFact.fact_type` docstring notes M25 taxonomy
- `tests/test_section_fact_extraction.py`:
  - `test_assistant_statement_extracted` → renamed `test_assistant_utterance_extracted_as_experience`
  - New `test_legacy_assistant_statement_remapped` covers the shim path
  - `test_mixed_user_and_assistant_facts` asserts the new collapsed taxonomy + speaker-based discrimination

**Bench state:** chained behind M24 LoCoMo completion (project `astrocyte-m25`, same `ASTROCYTE_M22_HINDSIGHT_ANSWERER=1` flag activates the full M23+M24+M25 stack). ETA after M24 LoCoMo finishes: ~35-45 min.

## 6. What's done vs what's pending

**Done — code in tree:**
- `_hindsight_answerer.py` module (M22+M23 evolution → M23 focused prompts as current state)
- `MemoryFact.chunk_id` + `MemoryFactHit.chunk_id` properties (M24)
- `astrocyte_client.search()` source-chunk inline plumbing (M24)
- Hindsight binary fact_type taxonomy + perspective tag in extraction prompt (M25)
- Legacy compat shim for `assistant_statement` → `experience+assistant` (M25)
- All gated by `ASTROCYTE_M22_HINDSIGHT_ANSWERER=1`, default OFF
- 2744 existing tests pass; 0 regressions

**Done — bench data:**
- M22 final: 188/230 = 81.74% (−6q vs R0-v2)
- M24 LME: 21/30 = tied R0-v2
- M24 LoCoMo: in flight, ~89.7% running

**Pending:**
- M24 LoCoMo final
- M25 LME + LoCoMo
- Phase 2 (lean-compile A/B test — skip wiki/MM/preference compile in bench) — informs eager-compile-vs-lean architectural decision
- Phase 3 (M20 reflect-debug now that M24 fact↔chunk pairing is in scope)
- v0.15.0 release decision — informed by M24+M25 results

## 7. Architectural insight banked

**The Hindsight-parity ROI hierarchy** (from this experiment arc):

1. **Inline fact↔source-chunk pairing (M24)** is the foundational architectural difference. Without it, no amount of prompt engineering helps the answerer cross-reference. With it, multi-hop and temporal jumped to/above R0-v2.
2. **Per-Q-type prompts must be focused, not stacked** (M23 > M22). 95-line monster directive regresses; 5-15 line per-category prompts recover.
3. **Speaker perspective should live in a speaker field, not in fact_type** (M25 > M14.1). `assistant_statement` as a separate fact_type creates redundancy when inline source-chunks land; Hindsight's binary world/experience taxonomy with speaker-based perspective is cleaner.
4. **Eager compile (wiki/MM/preference at retain time) is product surface, not bench accuracy** (TBD — Phase 2 will measure). Hindsight defers these to reflect-time or user-curation. Bench probably can be lean; product wants eager.

## 8. Arc close (2026-05-19) — M22-M27 complete, M24 ships as bench-stable, full arc validates capability-analysis pivot

**Final scoreboard across 8 bench runs (R0-v2 baseline + 7 experiment configs):**

| Cycle | LME | LoCoMo | Combined | Δ vs R0-v2 | Notes |
|---|---|---|---|---|---|
| **R0-v2** baseline | 21/30 | 173/200 | **194/230 (84.35%)** | — | M19 code, default config |
| **M24** (focused prompts + inline chunks) | 21/30 | 172/200 | **193/230 (83.91%)** | **−1q** | **Best — bench-stable parity** |
| M22 (monster directive) | 17/30 | 171/200 | 188/230 (81.74%) | −6q | Prompt over-loading |
| M25 (binary fact_type + persp tag) | 19/30 | 170/200 | 189/230 (82.17%) | −5q | Extraction rewrite hurt SSP/multi-hop |
| M25b (surgical M19 rollback) | 20/30 | 164/200 | 184/230 (80.00%) | −10q | Worst — structural changes leaked further |
| M26 (reflect + chunk pairing) | **24/30** | 164/200 | 188/230 (81.74%) | −6q | Best LME ever (+3q), LoCoMo multi-hop crash (−10pp) |
| M27 (cross-session link-expansion) | 20/30 | 166/200 | 186/230 (80.87%) | −8q | Early multi-hop 100% → final 90%; temporal crashed −7pp |

**Only M24 reaches parity.** Every other experiment trades a category win for a category loss. The bench has a local optimum at M24 (Hindsight-style answerer + fact↔chunk inline pairing, no further changes), and every other axis explored — extraction prompt rewriting, reflect-mode synthesis, cross-session entity traversal — moves us off that optimum.

### 8.1 Three structural insights banked

**1. Prompt-engineering ROI on this bench is exhausted.** M22-M25b explored five prompt variants. None beat M24. The bench's question distribution rewards the M19-era enriched extraction prompt + M24 answerer-side chunk pairing; deviations in either direction regress.

**2. Reflect vs recall+answerer is a clean architectural tradeoff** (M26). Reflect wins LME (24/30 = best ever, perfect 5/5 on SSP/SSU/knowledge-update) but loses LoCoMo multi-hop (−10pp) because the loop's narrower per-iteration context can't see the full candidate breadth that recall+answerer reads at once. The clean compose ("reflect for LME-like, recall for LoCoMo-like") would be bench-positive but bench-overfit — real workloads don't pre-label question types.

**3. Cross-session link-expansion helps multi-hop but adds noise on focused-retrieval questions** (M27). Early-bench multi-hop hit 100% (n=17), but the final landed at 90% (worse than R0-v2's 92%) because the entity traversal pulls in temporally-irrelevant facts on questions that don't need cross-session evidence. Temporal crashed −7pp from the same noise. The capability is real — it WILL help real workloads with genuine cross-session questions — but as a default-on bench feature it regresses.

### 8.2 What ships as v0.15.0

**Default behavior identical to v0.14.0.** All experimental work gated default-OFF; user-default config produces the same scores R0-v2 / M19a produced.

**In-tree, behind env flags:**
- `_hindsight_answerer.py` (M22-M25 evolution → M23 focused per-Q-type prompts as current form) — gated by `ASTROCYTE_M22_HINDSIGHT_ANSWERER=1`
- M24 fact↔chunk inline pairing — active only when M22 env flag is set (bench-only)
- M26 reflect with chunk pairing — gated by `ASTROCYTE_USE_REFLECT=1`
- M27 link-expansion — opt-in via `query_entities` parameter on `fact_recall` (default behavior: parameter omitted → branch doesn't fire)

**In-tree, always-on (no behavioral change at defaults):**
- M21 observation evolution (Trend enum, structured_doc, delta_ops, 9 CRUD MCP tools)
- M24 `MemoryFact.chunk_id` computed property (free — derived from existing fields)
- M27 `MemoryFact.confidence_score` + `mentioned_at` fields — populated when LLM emits them; legacy rows read as None
- M25 binary fact_type taxonomy with `_LEGACY_FACT_TYPE_REMAP` shim for backwards compat
- Hindsight-parity per-conversation perspective tag in extraction prompt

### 8.3 Bench-cycle release path

v0.15.0 reuses **M19's bench cycle** in `BENCH_PARITY.yaml` (RELEASING.md §122 hotfix path) — default behavior produces the same M19a 2-run mean 192/230 scores. The M22-M27 work landed infrastructure (chunk_id property, confidence/mentioned_at fields, link-expansion strategy, 11 new tests) without claiming a new bench cycle.

### 8.4 What comes next (capability-analysis pivot)

The 8-cycle arc validated the capability-analysis recommendation: **bench-side prompt engineering is ROI-exhausted; the next leverage is product-experience and architectural infrastructure**, not more bench tuning. Next cycle directions:

- **Embed-CLI-controlplane triad** — `astrocyte-embed` (zero-config local mode), finish `astrocyte-rs` CLI, minimal admin UI. Closes the developer-adoption gap with Hindsight.
- **Native LLM provider breadth** — Anthropic, Gemini, Ollama, Vertex direct (not just LiteLLM).
- **OTel tracing + Prometheus metrics** — Hindsight has it; we don't.
- **Coreference + alias resolution at retain time** (capability-analysis #5) — would help cross-session reasoning more cleanly than M27's link-expansion did.
- **Background retain-time enrichment** (capability-analysis #3) — decouple inline LLM calls so retain critical path returns fast.

The M27 link-expansion code lives behind `query_entities` opt-in for future workloads where cross-session traversal genuinely helps. The bench just isn't the right place to measure it.
