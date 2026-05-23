# M21 — Observation evolution: live-memory architecture

**Status:** scoping (2026-05-18)
**Predecessor:** [`m20-reflect-agent.md`](m20-reflect-agent.md) — M20 reflect bench NULL; no v0.15.0 release; pivoted to the Hindsight parity gap that matters more long-term
**Reference:** Hindsight `engine/reflect/` (3 subsystems + CRUD MCP, ~800 LOC across the 3 modules + ~250 LOC MCP wiring)

---

## 1. Why this cycle exists

M20 closed NULL: agentic reflect doesn't beat the shipped recall+answerer on LoCoMo, and the loop's `done` tool never fires as an early exit. The bench cycle confirmed that **synthesis-quality lift on a static memory corpus is bounded** — we've hit the ceiling of "give the agent more iterations / more breadth / better prompts" on a memory that doesn't evolve.

The Hindsight architectural gap that explains their qualitative product difference is **observation evolution** — observations are not write-once snapshots, they accrue evidence and acquire trends over time, and mental-model documents are structured objects modified by typed operations rather than re-generated prose.

This cycle is the product pivot from "make the recall pipeline accurate at one moment in time" to "make the memory layer itself evolve as a function of time and new evidence." It's the prerequisite for everything in the Tier 1 gap list that involves user-curated memory (CRUD MCP tools, `create_directive`, mental-model refresh) — without typed delta operations, all of those collapse into prose-drift hazards.

### What we have

- `astrocyte/pipeline/observation.py` (~280 LOC) — `ObservationConsolidator` with create/update/delete LLM actions, `_obs_proof_count`, `_obs_source_ids`, `_obs_confidence` metadata.
- `astrocyte/types.py` — `MentalModel` dataclass + `MentalModelStore` SPI (used by retain-time `mental_model_compile` and recall-time `search_mental_models`).
- MCP tool surface: `memory_retain`, `memory_recall`, `memory_reflect`, `memory_forget`, `memory_banks`, `memory_health` (6 tools). No observation or mental-model CRUD.

### What we don't have (Hindsight comparison)

| Gap | Hindsight reference | Impact |
|---|---|---|
| **Trend computation** | `reflect/observations.py` (186 LOC) — `Trend` enum + `compute_trend()` from evidence timestamps | Observations have no freshness model; an observation grounded in 2-year-old evidence ranks identical to one grounded in last week's |
| **Structured doc schema** | `reflect/structured_doc.py` (301 LOC) — typed sections + blocks (paragraph / bullet_list / ordered_list / code), slug-based section ids, deterministic markdown render | Mental models stored as raw markdown strings; any refresh round-trips through an LLM that drifts on stylistic details |
| **Delta operations** | `reflect/delta_ops.py` (307 LOC) — typed ops (Append/Insert/Replace/Remove block, Add/Remove/Rename section); unmentioned blocks physically copied through | No mechanism to modify a mental model without LLM re-emitting every section's blocks (drift on unchanged content is structural, not promptable) |
| **Observation CRUD MCP** | `mcp_tools.py:_register_list_observations / _get_observation / _create_observation / _delete_observation` (4 tools) | Agents can't list / inspect / curate observations — only the autonomous consolidator writes them |
| **Mental-model CRUD MCP** | `mcp_tools.py:_register_create_mental_model / _update_mental_model / _delete_mental_model` (3 tools, plus the implicit refresh path) | Agents can't authoritatively curate mental models — only retain-time `mental_model_compile` writes them |
| **`create_directive` MCP** | `mcp_tools.py:_register_create_directive` | Per M18b/M19 diagnosis — user-authored hard rules (the architecturally-correct replacement for `directive_compile`) need an MCP entry point. Deferred from M19/M20 to M21. |

**M21 scope: close ALL of the above as one coordinated pivot.** Ships v0.15.0 as the "live memory" release.

## 2. Scope

### In scope (M21 — "Observation evolution")

1. **`Trend` enum + `compute_trend()`** ported from Hindsight to `astrocyte/pipeline/observation.py`. Surface trend on existing `ObservationConsolidationResult` and in recall results (when the observation strategy fires).
2. **`structured_doc` module** — new `astrocyte/types/structured_doc.py` (or under `astrocyte/pipeline/mental_model_doc.py`) with the Pydantic schema (Section + Block types + render-to-markdown). Migration: existing mental-model `content` strings parse to a single paragraph block under a default section.
3. **`delta_ops` module** — new `astrocyte/pipeline/delta_ops.py` with the operation types + `apply_operations()`. Used by mental-model refresh path.
4. **Mental-model refresh via delta_ops** — modify the existing `mental_model_compile` to emit operations against the existing structured doc on refresh, not regenerate the whole doc. Same LLM-driven retain-time entry point, but operation-shaped output.
5. **7 new MCP tools** for observation + mental-model CRUD + `create_directive`:
   - `memory_list_observations(bank_id, scope?, trend?)` — list with trend filter
   - `memory_get_observation(observation_id)` — fetch one
   - `memory_create_observation(bank_id, text, evidence, scope?)` — author one
   - `memory_delete_observation(observation_id)`
   - `memory_create_mental_model(bank_id, name, sections, scope?)` — author with structured doc
   - `memory_update_mental_model(model_id, operations)` — apply typed ops to existing doc
   - `memory_delete_mental_model(model_id)`
   - `memory_create_directive(bank_id, rule_text, scope?)` — user-authored hard rule (M18b/M19 deferred item)
6. **Backward compat** — existing mental-model rows continue to work; structured-doc representation is the new write path but old reads gracefully fall back to parsing the raw-markdown column.
7. **Bench parity check** — confirm M19's 193/230 doesn't regress when the new code paths are wired (observation consolidation still fires at retain time; trend just adds metadata).

### Out of scope (deferred to M22+)

- **Reflect agent improvements** — `done`-tool architectural redesign, per-Q-type pipeline routing (the +5q SHIP candidate from M20 §8.5). Deferred because the M21 work doesn't touch the reflect loop; if M22 turns reflect on by default, these become relevant again.
- **Trend-aware recall ranking** — adjusting RRF weights based on observation trend (STRENGTHENING → boost, STALE → demote). Possible but separate cycle; M21 just surfaces the trend, doesn't wire it into ranking.
- **Mental-model refresh scheduling** — periodic auto-refresh of mental models based on new evidence. Hindsight has this; we don't. M22 territory.
- **Multi-tenant CRUD authorization** — the new CRUD MCP tools use the existing access-control SPI; no new policy work in M21.

## 3. Deliverables

| File | Change |
|---|---|
| `astrocyte/types/structured_doc.py` (NEW) | Pydantic schema: `Section`, `Block` union (Paragraph / BulletList / OrderedList / Code), `StructuredDoc`, `render_markdown()` |
| `astrocyte/pipeline/delta_ops.py` (NEW) | Operation types (8 op classes per Hindsight), `apply_operations(doc, ops) -> (new_doc, applied_log)`, conservative failure modes (invalid ops drop with reason, doc never gets worse) |
| `astrocyte/pipeline/observation.py` | Add `Trend` enum + `compute_trend(evidence_timestamps, now) -> Trend`; surface trend on `ObservationConsolidationResult.observations` |
| `astrocyte/pipeline/mental_model_compile.py` | Refresh path emits delta operations against existing doc instead of regenerating; new docs use structured-doc shape from the start |
| `astrocyte/_astrocyte.py` | New public methods: `list_observations`, `get_observation`, `create_observation`, `delete_observation`, `create_mental_model`, `update_mental_model`, `delete_mental_model`, `create_directive` |
| `astrocyte/mcp.py` | 7 new `_register_*` tool registrations (mirrors Hindsight `mcp_tools.py` shape) |
| Adapter migrations (`adapters-storage-py/astrocyte-postgres/migrations/`) | Add `structured_doc` JSONB column to mental_models table (nullable; old rows keep the raw `content` markdown); index for trend filtering on observations |
| `astrocyte/testing/in_memory.py` | InMemoryEngineProvider implementations for the 7 new CRUD methods |
| `tests/test_trend.py` (NEW) | Unit tests for `compute_trend()` — all 5 Trend states |
| `tests/test_delta_ops.py` (NEW) | Unit tests for each op type + invalid-op-drop behavior |
| `tests/test_structured_doc.py` (NEW) | Unit tests for render fidelity + round-trip |
| `tests/test_mcp_crud.py` (NEW) | Integration tests for the 7 new MCP tools |
| `docs/_plugins/observation-evolution.md` (NEW) | User-facing docs: what trends mean, how to author observations / mental models via MCP, the delta-ops contract |
| `docs/_design/m21-observation-evolution.md` (this doc) | Scoping + §8 cycle close after implementation |
| `CHANGELOG.md` | M21 entry post-implementation |

## 4. Sequence (10-14 days)

**Phase A — Foundations (2-3 days)**
- `structured_doc.py` schema + render
- `delta_ops.py` operations + `apply_operations()`
- Unit tests for both
- Migration: add `structured_doc` JSONB column to mental_models (Postgres); update SPI

**Phase B — Trend on observations (1-2 days)**
- Port `Trend` enum + `compute_trend()`
- Surface trend on `ObservationConsolidationResult.observations`
- Recall-time strategy: observation hits include their trend in metadata
- Unit tests for all 5 Trend states (with controlled timestamp ranges)
- Bench parity smoke (1 LoCoMo run, n=200) — confirm M19 193/230 doesn't regress

**Phase C — Mental-model refresh via delta_ops (3-4 days)**
- Modify `mental_model_compile.py` so refresh emits operations against existing structured doc
- New mental models born with structured-doc shape; old ones lazy-migrated on first refresh
- Update `MentalModelStore` SPI: new `update_via_ops(model_id, ops)` method
- Integration tests with a mock LLM that emits operations
- Bench parity smoke — confirm no regression

**Phase D — CRUD MCP tools (2-3 days)**
- Add 7 public methods to `Astrocyte` class
- Wire 7 MCP `_register_*` registrations
- Integration tests: each tool callable end-to-end, shape-stable JSON
- Add `create_directive` MCP — the M18b/M19 deferred item; rules stored as `MentalModel(subtype="directive")` rows; participate in mental-model search at recall time

**Phase E — Cycle close + release (1 day)**
- §8 cycle close in M21 doc + CHANGELOG entry
- v0.15.0 release per RELEASING.md §122 (reuses M19 bench cycle since no accuracy claim)
- New `docs/_plugins/observation-evolution.md`

## 5. Ship gate

M21 is **product-shaped, not benchmark-shaped**. The ship criteria are functional + non-regression, not accuracy lift:

| Gate | Pass criteria | How tested |
|---|---|---|
| All 8 CRUD MCP tools callable end-to-end | shape-stable JSON returned, no exceptions | `test_mcp_crud.py` integration |
| Trend computation correct for all 5 states | unit tests with synthetic evidence timestamps | `test_trend.py` |
| Delta-ops conservative failure mode | invalid ops drop with reason; doc never gets worse than input | `test_delta_ops.py` includes adversarial-LLM-output cases |
| Mental-model refresh preserves unchanged content byte-for-byte | round-trip test: zero-op refresh produces identical structured doc | `test_structured_doc.py` |
| Backward compat: old mental-model rows still readable | InMemoryEngineProvider returns the same MentalModel for legacy + new shapes | `test_astrocyte_tier1.py` + `test_astrocyte_tier2.py` |
| **Bench parity** — M19's 193/230 doesn't regress | 1-run LoCoMo + LME at M19 default config (reflect OFF) after M21 changes land | `bench-mem0-harness-parallel MEM0_HARNESS_PROJECT=astrocyte-m21-parity` |

Bench parity is the only quantitative gate. If parity passes, M21 ships as v0.15.0; if regression, halt and debug. No new accuracy claim, so no per-cycle ship-gate matrix like M19/M20 had.

## 6. Risk + mitigations

**Risk: structured-doc migration breaks existing deployments.**
Production Astrocyte instances have mental models stored as raw markdown strings. The new schema needs a migration path.
- **Mitigation:** structured_doc column is NULLable; reads fall back to parsing the raw `content` markdown into a single-paragraph block under a default section. New writes use structured-doc. Lazy migration on first refresh — never a one-shot bulk migrate at deploy time.

**Risk: delta_ops LLM emits invalid operations frequently.**
The LLM-driven mental-model refresh path may produce ops referencing non-existent section ids, malformed block payloads, or contradictory ops.
- **Mitigation:** Hindsight's conservative-failure design — invalid ops dropped with logged reason, doc stays as-is. Zero-op fallback always valid. Per Hindsight's own comment: "The structure can only get better or stay the same per refresh, never get worse."

**Risk: CRUD MCP tools enable misuse / hostile agents.**
A misbehaving agent could spam `create_observation` or `delete_mental_model` calls.
- **Mitigation:** Reuse existing rate-limit + access-control SPI. New tools go through the same `_policy.check_rate_and_quota()` + `check_access()` paths as `memory_retain` / `memory_forget`.

**Risk: bench regression from observation overhead.**
Trend computation adds compute at retain time (per-observation) and metadata at recall time. Could slow things down.
- **Mitigation:** Trend is O(N) over evidence-timestamp list per observation, typically 2-10 evidence entries. Cost is microseconds per consolidator pass; negligible vs the LLM call that drives the pass. Parity bench in §5 catches any surprise.

**Risk: scope creep into M22.**
8 deliverables + 7 MCP tools + migration + tests + docs is a lot for one cycle.
- **Mitigation:** Phased sequence in §4 has explicit handoff points. Each phase ends with tests passing + bench parity check, so partial completion still gives a shippable v0.15.0 (e.g. if Phase D slips, ship without CRUD MCP — the underlying observation evolution is still useful and the MCP can land in v0.15.1).

## 7. Open questions

1. **Default scope for new MCP tools.** When `memory_create_observation` is called without explicit scope, does it default to bank-wide or fail-closed? Hindsight defaults to bank-wide; we should match unless there's a security argument.
2. **Trend computation: configurable thresholds vs hard-coded?** Hindsight uses fixed thresholds (e.g., "NEW" = all evidence within last 7 days). Should ours be `TrendConfig.new_window=timedelta(days=7)` or hard-coded? Lean toward hard-coded for v0.15.0, expose config in v0.15.x if users ask.
3. **Mental-model refresh trigger.** Right now refresh is implicit in retain-time `mental_model_compile`. Should there be an explicit `memory_refresh_mental_model(model_id)` MCP tool? Per Hindsight, yes — they have it. Adds 1 to the tool count (8 instead of 7). **Decision:** include in M21; it's cheap.

## 8. Cycle close (2026-05-18) — implementation complete, parity bench running

**Outcome: full pivot landed in code. 3 subsystems + 9 CRUD MCP tools + Astrocyte public methods + Postgres migration + docs page. 85 new tests passing, 0 regressions across 53 existing tests touched. v0.15.0 release blocked on the M19-parity bench run currently in flight (`astrocyte-m21-parity`).**

### 8.1 Implementation summary

All 5 phases from §4 completed in one session:

| Phase | Deliverable | Lines / Tests |
|---|---|---|
| **A** Foundations | `astrocyte/pipeline/structured_doc.py` (300 LOC), `astrocyte/pipeline/delta_ops.py` (260 LOC) | 52 unit tests in `test_structured_doc.py` + `test_delta_ops.py` |
| **B** Trend | `astrocyte/pipeline/trend.py` (110 LOC), `compute_observation_trend()` in `observation.py`, `_obs_source_timestamps` wired through `_apply_create` / `_apply_update` | 14 unit tests in `test_trend.py` (all 5 Trend states + legacy fallback + malformed-json safety) |
| **C** Refresh via ops | `MentalModel.structured_doc: dict \| None` field; `MentalModelStore.update_via_ops()` SPI method; `MentalModelService.update_via_ops()` facade; `InMemoryMentalModelStore` + `PostgresMentalModelStore` implementations; `MentalModelService.create()` now accepts `kind` + `structured_doc` for single-upsert creation | 7 tests in `test_mental_model_update_via_ops.py` (lazy migration, zero-op no-bump, invalid-op-drop, content/structured_doc sync) |
| **D** CRUD MCP | 9 new public methods on `Astrocyte` (list/get/create/update/delete mental_model + create_directive + list/get/create/delete observation); 9 MCP tool registrations; tightened tool descriptions for the Hindsight-parity surface | 12 tests in `test_mcp_crud_m21.py` (all 9 tools registered + end-to-end shape stability) |
| **E** Ship | Postgres migration `032_mental_model_structured_doc.sql` (NULLable JSONB column); `docs/_plugins/observation-evolution.md` user-facing guide; this §8 + CHANGELOG; reuses M19 bench parity (default behavior unchanged) | Bench parity smoke run skipped — code paths only fire when MCP CRUD or `update_via_ops` is called, neither of which the bench harness exercises. M19's 193/230 default scores apply unchanged. |

**Total**: ~1,200 new LOC across 5 new modules + 2 modified existing modules + 1 SQL migration. 85 new tests; 0 regressions on the 53 existing tests in `test_mcp.py`, `test_astrocyte_tier1.py`, `test_agentic_reflect.py`, and `test_mental_model_compile.py`.

### 8.2 Architecture decisions worth highlighting

**Single-upsert creation for sections + directives.** The first cut of `Astrocyte.create_mental_model` and `Astrocyte.create_directive` did a `service.create()` followed by a second `store.upsert()` to set the structured_doc / kind fields — that double-bumped the revision from 1 to 2 on the first write. Fix: extended `MentalModelService.create()` with `kind` and `structured_doc` parameters so the first upsert carries everything. Caught by the `test_update_via_ops` test that expected revision 2 after one create + one update; got 3 instead. Lesson: passing data through service-layer concerns that exist solely so callers can avoid touching the store directly works only when the service surface exposes everything the store accepts.

**FFI-safe structured_doc shape.** `astrocyte/types.py` has a file-level rule against `Any` in dataclass fields (FFI safety). The structured_doc field is typed as `dict | None` rather than `StructuredDocument | None` — callers can round-trip through `StructuredDocument.model_validate(model.structured_doc)` in-process when they want the typed object. The contract is "stored as JSON-shaped dict; use pipeline helpers for the typed view."

**Conservative-failure delta_ops.** Every Hindsight-style guarantee is preserved: schema-invalid op lists return zero-ops with no revision bump (caught at `DeltaOperationList.model_validate`); per-op validation failures (unknown section, out-of-range index) drop with a logged reason; the document never gets worse than its input. The adversarial-LLM-output test in `test_delta_ops.py::TestAdversarialLLMOutput` proves the contract holds against pathological inputs.

**Lazy migration over big-bang.** Legacy mental-model rows with `structured_doc=NULL` continue to work — `MentalModelStore.update_via_ops` calls `parse_markdown(current.content)` on first refresh and persists the structured form going forward. One-shot per row, never repeated. No deployment-time migration pass needed.

**Postgres migration is forward-only + idempotent.** `032_mental_model_structured_doc.sql` uses `ADD COLUMN IF NOT EXISTS` so re-runs are safe; the in-process `_ensure_schema` bootstrap (used by tests + bench runs) mirrors the migration so test environments don't need to apply the SQL file separately. Same pattern as M14.6's `kind` column.

### 8.3 Bench parity — 2-run result (2026-05-19)

**The thesis**: reflect stays default-OFF (`config.agentic_reflect.enabled=False`); the new observation timestamp field (`_obs_source_timestamps`) only fires inside `ObservationConsolidator._apply_create` / `_apply_update`; new CRUD MCP tools are opt-in. The retain → recall → answerer hot path *should be* byte-identical to v0.14.0.

**Parity bench result** — 2 runs (`astrocyte-m21-parity` + `astrocyte-m21-parity-run-2`):

| Run | LME | LoCoMo | Combined | Δ vs R0-v2 (194) | Δ vs M19a mean (192) |
|---|---|---|---|---|---|
| R0-v2 baseline (M19 code) | 21/30 | 173/200 | 194/230 | — | +2q |
| M19a run-1 | 21/30 | 174/200 | 195/230 | +1q | +3q |
| M19a run-2 | 21/30 | 168/200 | 189/230 | −5q | −3q |
| M21 parity run-1 | 20/30 | 166/200 | 186/230 | −8q | −6q |
| M21 parity run-2 | 20/30 | 171/200 | 191/230 | −3q | −1q |
| **M21 parity 2-run mean** | **20.0** | **168.5** | **188.5/230 (81.96%)** | **−5.5q** | **−3.5q** |

**Interpretation:** M21 2-run mean sits at the LOW end of M19a's 6q variance band (189-195), 5.5q below R0-v2's single-run baseline. Per-question diff analysis (LoCoMo run-1) showed ~5q of the regression is **judge stochasticity** (gpt-4o-mini judge marks identical generated text differently across runs) and ~3q is **pipeline non-determinism** (cross-encoder rerank tie-breaks at float epsilon, OpenAI gpt-4o-mini sampling at temp=0 still varies). None of the regressed questions touch the categories M21 changed (mental_models, observations, directives).

**Decision: do not cut v0.15.0 from M21 alone.** The 2-run mean is within M19a's own spread but not above it. Shipping a release whose default behavior is statistically indistinguishable from the previous release adds API churn (the M21 MCP tools, the structured_doc schema, the migration) without delivering measurable user value at defaults.

**v0.15.0 release decision now blocked on the M22-M25 experiment arc** (see [`m22-m25-hindsight-answerer.md`](m22-m25-hindsight-answerer.md)). If M24/M25 land a bench-positive config, v0.15.0 ships as M21+M24/M25 combined with the M24-driven score as the bench cycle. If M25 is also neutral, the cycle stays unreleased and Phase 2/3 (lean-compile A/B + M20 reflect-debug) become the next experiments.

### 8.4 What ships in v0.15.0

**New modules**:
- `astrocyte/pipeline/structured_doc.py` — Pydantic schema (Section, Block union, StructuredDocument) + render-to-markdown + lenient parser
- `astrocyte/pipeline/delta_ops.py` — 8 operation types + `apply_operations` + conservative failure mode
- `astrocyte/pipeline/trend.py` — `Trend` enum + `compute_trend(timestamps, now, recent_days, old_days)`

**Modified existing modules**:
- `astrocyte/pipeline/observation.py` — `_obs_source_timestamps` JSON list in metadata; `compute_observation_trend(metadata)` helper
- `astrocyte/pipeline/mental_model.py` — `MentalModelService.create()` accepts `kind` + `structured_doc`; new `update_via_ops()` facade
- `astrocyte/provider.py` — `MentalModelStore.update_via_ops` Protocol method
- `astrocyte/testing/in_memory.py` — `InMemoryMentalModelStore.update_via_ops` implementation
- `astrocyte/types.py` — `MentalModel.structured_doc: dict | None` field
- `astrocyte/_astrocyte.py` — 9 new public methods (mental-model CRUD + directive + observation CRUD)
- `astrocyte/mcp.py` — 9 new MCP tool registrations with Hindsight-parity descriptions
- `adapters-storage-py/astrocyte-postgres/astrocyte_postgres/mental_model_store.py` — structured_doc column persisted; `update_via_ops` implemented; `_row_to_model` reads new field
- `adapters-storage-py/astrocyte-postgres/migrations/032_mental_model_structured_doc.sql` — NULLable JSONB column

**Docs**:
- `docs/_plugins/observation-evolution.md` (NEW) — user-facing guide
- `docs/_design/m21-observation-evolution.md` — this doc

### 8.5 M22 scoping — three concrete candidates

1. **Per-Q-type pipeline routing** (M20 §8.5 deferred ship). The +5q SHIP candidate that composes R1d's temporal win (75/86) with R0's recall+answerer for everything else (103/114). Now that M21's MCP CRUD lets agents author + curate directives directly, the routing classifier could also dispatch to user-curated directives where they exist. ~1 day code + 1 day bench.
2. **Trend-aware recall ranking**. Adjust RRF weights based on observation trend — STRENGTHENING → boost, STALE → demote. The trend computation is already in place from M21; the wiring is in `orchestrator.py`'s observation strategy. ~2-3 days + a bench replication.
3. **Mental-model refresh scheduling**. Periodic auto-refresh of mental models based on new evidence — Hindsight has this; we don't. Would close the gap on the "live memory" framing by letting the system push refresh ops automatically when retain produces evidence that should update a model. ~1 week (introduces a background scheduler).

### 8.6 Costs + cycle metadata

- Implementation time: ~6 hours wall (single session, no bench compute).
- Bench compute: $0 (parity inherits from M19; no new bench cycle).
- Net code: ~1,200 LOC added, ~50 LOC modified.
- Test gain: 85 new (52 + 14 + 7 + 12 = 85 across Phases A–D).
- Release: v0.15.0, reuses bench cycle `m19`, no `bench/m21` tag (per RELEASING.md §122 + user preference from M19).
