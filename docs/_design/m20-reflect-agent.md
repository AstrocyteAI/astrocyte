# M20 — Reflect Agent: ship the surface + measure the lift

**Status:** scoping (2026-05-18)
**Predecessor:** [`m19-prompt-routing.md`](m19-prompt-routing.md) — M19a shipped v0.14.0 (2-run mean 193/230 = 83.91%)
**Reference:** Hindsight `engine/reflect/` (3547 LOC across 9 files); we already have parity in `astrocyte/pipeline/agentic_reflect.py` (864 LOC)

---

## 1. Why this cycle exists

We built a Hindsight-parity native-function-calling reflect agent in `astrocyte/pipeline/agentic_reflect.py` (~864 LOC) but **never wired it to any user-facing surface, never benched it, and never shipped it**. M15's null verdict was on a non-agentic reflect (single-call recall + synthesis); the agentic loop we have now is fundamentally different and untested.

### What we have

- `agentic_reflect.py` (864 LOC) — native function-calling loop, hierarchical tools (recall, search_observations, expand, done), token tracking, citation validation, tool-name normalization
- `observation.py` — `ObservationConsolidator` for evidence-grounded observations
- `reflect.py` (295 LOC) — fallback recall + synthesis (M15 shape)
- `section_reflect.py` (290 LOC) — section-grain adapter

### What we don't have

| Gap | Hindsight reference | Impact |
|---|---|---|
| **MCP `reflect` tool** | `mcp_tools.py:_register_reflect` | Agents can't invoke reflect at all |
| **Public `Astrocyte.reflect()` API** | `MemoryEngine.reflect()` | No core method for non-bench callers |
| **Bench coverage** | Hindsight benches reflect on hard categories (multi-hop, knowledge-update) | We don't know if our agentic_reflect lifts accuracy |
| **`delta_ops`** | `reflect/delta_ops.py` (307 LOC) — evidence add/remove/strengthen ops | Observations can't evolve over time |
| **Trend computation** | `reflect/observations.py` (186 LOC) — algorithmic STABLE/STRENGTHENING/WEAKENING/NEW/STALE | Observations have no freshness model |
| **Structured doc output** | `reflect/structured_doc.py` (301 LOC) — sections + memory-id citations | Reflect returns free text only |
| **Prompt library** | `reflect/prompts.py` (725 LOC) — curated reflect prompts | Ours scattered in agentic_reflect.py |

**M20 scope: close the surface gap + validate accuracy. Defer Trends + delta_ops + structured_doc to M21.**

## 2. Scope

### In scope (M20b — "Surface ship + bench validation")

1. **Public `Astrocyte.reflect()` API** on `astrocyte/_astrocyte.py`
2. **MCP `_register_reflect`** tool surface in `astrocyte/mcp.py`
3. **Bench harness integration** — `ASTROCYTE_USE_REFLECT=1` env var routes the bench through reflect instead of plain recall
4. **2-run reflect bench** against v0.14.0 baseline (LME-30 + LoCoMo-200)
5. **Cycle close** — ship/null verdict with full per-category data

### Out of scope (deferred)

- **Trend computation** (`observations.py` parity) → M21 if reflect bench is positive
- **`delta_ops`** (observation evolution) → M21
- **`structured_doc`** (report-shaped output) → M21
- **Observations CRUD MCP tools** (`list_observations`, `get_observation`, `create_observation`, `delete_observation`) → M21
- **Mental Models CRUD MCP tools** → separate M21/M22 cycle (already in Tier 1 gap list)
- **`create_directive` MCP tool** → separate M21 cycle (per prior plan)
- **Reflect prompt library refactor** (collect into reflect/prompts.py module) → M21 if reflect ships

## 3. Deliverables

| File | Change |
|---|---|
| `astrocyte/_astrocyte.py` | New `reflect(bank_id, query, *, budget=None, max_iterations=10) -> ReflectResult` method delegating to `agentic_reflect.agentic_reflect()` |
| `astrocyte/mcp.py` | New `_register_reflect(server, astrocyte)` MCP tool handler mirroring Hindsight's signature: input `{bank_id, query, context?, budget?}`, output `{answer, sources, iterations, tools_called}` |
| `mcp-plugins/astrocyte/` plugin manifests | Bump version; document new `reflect` tool capability |
| `scripts/mem0_harness/astrocyte_client.py` | New `ASTROCYTE_USE_REFLECT=1` branch: when set, after building candidates skip cross_encoder_rerank + answerer and instead call `astrocyte.reflect(query)`. Default OFF. |
| `tests/test_reflect_public_api.py` (NEW) | Unit test: `Astrocyte.reflect()` returns a `ReflectResult`; gracefully handles empty bank |
| `tests/test_mcp_reflect.py` (NEW) | Integration test: MCP `reflect` tool callable; returns shape-stable JSON |
| `docs/_design/m20-reflect-agent.md` | This doc + §8 cycle close after bench |
| `CHANGELOG.md` | M20 entry post-bench: SHIP or NULL verdict |

## 4. Bench plan

| Run | Config | Project | Decision |
|---|---|---|---|
| **R0 (baseline)** | v0.14.0 defaults (B1-dp+RRF + per-Q-type prompt), reflect OFF | `astrocyte-m20-baseline-run-1` | Sanity — should reproduce v0.14.0's 193/230 |
| **R1 (reflect ON)** | v0.14.0 defaults + `ASTROCYTE_USE_REFLECT=1` | `astrocyte-m20-reflect-run-1` | Δ vs R0 = reflect's contribution |
| **R2 (reflect ON, replication)** | Same as R1 | `astrocyte-m20-reflect-run-2` | 2-run mean for ship decision |

Budget: ~$6 (3 runs × ~$2), ~75 min total.

## 5. Ship gate

**Decision matrix from R1 + R2 2-run mean:**

| Combined Δ vs v0.14.0 (193/230) | Verdict | Action |
|---|---|---|
| **≥+5q (+2.2pp)** | SHIP as default-on | Flip `ASTROCYTE_USE_REFLECT` default to ON in bench; tag v0.15.0 with bench label `m20` |
| **+2q to +5q** | SHIP as opt-in | Public API + MCP tool ship; default OFF; documented as "use for hard questions" |
| **−2q to +2q** | NULL — surface ship only | Public API + MCP tool ship (agents can call); document as no-bench-lift; defer Trend / delta_ops to M21 |
| **< −2q** | INVESTIGATE | Reflect actively hurting bench accuracy; check for prompt-routing interaction (M19a was tuned for direct recall, may conflict with reflect) |

**Latency budget check:** reflect can issue up to `max_iterations` (default 10) LLM calls per question. Bench latency may go from ~25s/q to 60-120s/q. Acceptable for accuracy lift; documented if shipping default-on.

## 6. Sequence (1 week)

**Day 1 — Public API**
- Implement `Astrocyte.reflect()` core method
- Wire to `agentic_reflect.agentic_reflect()` with proper tool callbacks (recall_fn, search_observations_fn, expand_fn)
- Unit test: empty bank, basic call, citation extraction

**Day 2 — MCP tool**
- Add `_register_reflect` in `astrocyte/mcp.py`
- Bump mcp-plugins/astrocyte plugin version (4 manifest files per RELEASING.md)
- Integration test: `astrocyte-mcp` server handles reflect call end-to-end

**Day 3 — Bench harness integration**
- `astrocyte_client.py` `ASTROCYTE_USE_REFLECT=1` branch
- 1-question smoke run to verify the bench harness handles reflect's output shape
- Verify reflect's `sources` field maps correctly to bench's `memories` field for per-cutoff scoring

**Day 4-5 — Bench validation**
- R0 (baseline parity check) — 1 run
- R1 + R2 (reflect ON, replication) — 2 runs
- Analyze per-category: where does reflect help (multi-hop expected), where does it hurt (latency on simple questions)

**Day 6 — Cycle close**
- Update CHANGELOG.md + §8 of this doc with bench results
- If SHIP: release commands per RELEASING.md (v0.15.0, cycle m20)
- If NULL: M21 scoping doc — pivot to Trend / delta_ops which need observations to evolve

## 7. Risk + mitigations

**Risk: reflect output shape doesn't match bench answerer expectations.**
The bench's per-cutoff scoring (top_10/20/50/200) expects a list of `{memory, score, id}` candidates the upstream answerer can consume. Reflect returns a single composed answer + sources. The bench's top_K scoring becomes "did reflect produce the right answer" rather than "did the right memory rank high."
- **Mitigation:** in Day 3, decide either (a) the bench's `top_K` becomes "reflect's confidence-weighted answer in top K iterations" or (b) we score reflect's `sources` as the candidate list. Option (b) preserves comparability with v0.14.0.

**Risk: latency makes bench impractical.**
10 iterations × ~5s LLM call = ~50s/q. At 230 questions × 2 runs = 460 questions, that's ~6-7 hours per bench leg.
- **Mitigation:** set `max_iterations=5` for the bench (Hindsight default is 10). Validate latency on the smoke run before committing to full bench.

**Risk: reflect double-dips on questions our shipped recall already solves.**
v0.14.0 already gets 84.25% LoCoMo. The hard categories (multi-hop, knowledge-update) are where reflect should help. If reflect runs on EVERY question, the easy 80% may regress from added LLM noise.
- **Mitigation:** consider per-Q-type routing (M19a pattern) — only invoke reflect for hard categories. Out of scope for M20b but if R1 shows mixed signal this becomes M21 priority.

**Risk: `agentic_reflect.py` has latent bugs (never benched).**
The code was ported from Hindsight pattern but never exercised at scale.
- **Mitigation:** Day 3 smoke run catches catastrophic failures before R1/R2 bench cost.

## 8. Cycle close (2026-05-18) — NULL verdict, NO release cut (continue to M21)

**Outcome: NULL on bench (reflect regresses LoCoMo by −6 to −12q across 4 configs). Decision: do not release v0.15.0. Code lands in-tree as evidence + foundation for M21 per-Q-type pipeline routing (the +5q SHIP candidate from §8.5).**

**Rationale for no release:** the surface (`Astrocyte.reflect()` agentic path + MCP `memory_reflect` + bench knobs) adds no measurable user value at default config — reflect default-OFF means v0.15.0 would produce the identical 193/230 score as v0.14.0. Shipping a public API + MCP tool that underperforms the existing recall+answerer pipeline would only add API churn for downstream callers without giving them a reason to flip the flag. Better to wait for M21 to produce a config that actually beats v0.14.0 (per the per-Q-type pipeline routing hypothesis below) and release v0.15.0 with a real lift attached.

### 8.1 Bench data (all LoCoMo n=200, max-questions=20/conv × 10 convs)

| Run | Config | Total | mh | od | sh | t | Δ vs R0 |
|---|---|---|---|---|---|---|---|
| **R0** | recall+answerer (M19a defaults, reflect OFF) | **173/200 (86.5%)** | 73/79 | 26/31 | 4/4 | 70/86 | — |
| R1 | reflect ON, generic prompt, seed top-50, max_iter=5 | 161/200 (80.5%) | 61/79 | 22/31 | 4/4 | 74/86 | **−12q** |
| R1b | + M19a per-Q-type routing in reflect's system prompt | 162/200 (81.0%) | 62/79 | 25/31 | 4/4 | 71/86 | **−11q** |
| R1c | + wide seed (top-200, parity with R0's view) | 161/200 (80.5%) | **66/79** | 21/31 | 4/4 | 70/86 | **−12q** |
| R1d | + max_iter=10 + STOP-EARLY discipline rule | **167/200 (83.5%)** | 66/79 | 22/31 | 4/4 | **75/86** | **−6q** |

**R0 LME baseline (reflect OFF, n=30): 21/30 = 70.0%**, identical to both M19a runs. R0 combined 21+173 = **194/230 (84.35%)** — within +2q of M19a's 2-run mean (192/230), parity confirmed; Day 3 harness changes (Hindsight integrated-mode rewrite) are non-regressing.

R1/R1b/R1c/R1d were LoCoMo-only since the regression signal was unambiguous on LoCoMo alone and the combined-bench ship gate only triggers when LoCoMo wins. R1d projected combined (assuming LME parity with R0): 21+167 = 188/230 = 81.7%, −6q below R0 — squarely in M20 §5 ship-gate's **"< −2q INVESTIGATE"** territory.

### 8.2 Three falsified hypotheses + one confirmed pathology

**H1 — "Per-Q-type prompt routing inside reflect will recover M19a's +12q multi-hop lift" → FALSIFIED (R1b vs R1: +1q overall, +1q multi-hop).** The agent reads the routing blocks (verified via tests asserting system-prompt content) but doesn't translate them into tool-call decisions or final-answer composition. Prompt-level instruction is structurally weaker inside a tool-using loop than inside a single-shot answerer.

**H2 — "Wider seed will close the multi-hop gap (reflect was info-limited, not synthesis-limited)" → PARTIALLY FALSIFIED (R1c vs R1b: multi-hop +4q, open-domain −4q, net zero).** Wider seed DOES feed more multi-hop bridge memories into the agent's context (+4q multi-hop is a real signal that persisted in R1d). But the same wider context hurts open-domain by −4q — the agent gets distracted by irrelevant candidates on focus questions. The fix isn't "wide seed always," it's per-Q-type seed sizing — M21 territory.

**H3 — "STOP-EARLY rule + iteration headroom will let the loop converge productively" → STRUCTURALLY FALSIFIED on stop-early, partially confirmed on iteration headroom.** R1d iter distribution: `{10: 200}` — every question hit max. R1c was `{5: 200}` at its cap. **The agent CANNOT self-terminate via the `done` tool**, regardless of how explicit the system-prompt guidance. This is the cleanest M21 signal of the cycle. The +5q lift from doubling the iteration cap (R1d vs R1c) came entirely from "more useful retrieval per question," concentrated in temporal (+5q) where iterative date-narrowing pays off. Multi-hop didn't budge with more iterations — confirming H2's diagnosis that synthesis (not retrieval) is the multi-hop bottleneck.

**Pathology confirmed: `done` tool never fires as an early exit. Loop runs to max_iter on 100% of questions across all four configs.** This is the M21 priority — either redesign the tool (force `done` after N tools, or make every iteration produce a candidate answer the loop can commit on), or accept that reflect's loop is "max_iter LLM calls + final synthesis" with no convergence behavior.

### 8.3 Where reflect actually wins

Across all four reflect configs, **temporal-reasoning consistently beats R0**:
- R1: 74/86 (+4q vs R0's 70/86)
- R1b: 71/86 (+1q)
- R1c: 70/86 (0q)
- R1d: 75/86 (+5q)

Iterative date-narrowing pays off when the question needs an OLDER session that single-shot recall missed. This is the per-Q-type pipeline routing hypothesis for M21: **use reflect for temporal, recall+answerer for everything else.** Projected lift if we composed R1d temporal (75/86) with R0 everything-else (103/114): 75+103 = 178/200 = 89.0%, **+5q above R0 baseline.** That would clear the M20 §5 ship gate (+5q → SHIP default-on) — but it requires per-Q-type pipeline routing in `AstrocyteClient.search()`, which is M21 work.

### 8.4 What ships in v0.15.0

**Surface (no bench-measured lift):**
- `Astrocyte.reflect()` public API — already shipped via M20 Day 1 (config-driven via `agentic_reflect.enabled=True`).
- MCP `memory_reflect` tool — already shipped with tightened description that explicitly forbids the double-synthesis anti-pattern (don't pass `answer` back through another LLM for re-synthesis).
- `docs/_plugins/recall-vs-reflect.md` — the contract: `recall()` for raw memories (agent owns synthesis), `reflect()` for finished answers (Astrocyte owns synthesis), never chain them through a second LLM. Frames the M20 finding as a positive learning: the bench harness's first cut chained reflect through the bench answerer and that wasn't a fair test of either pattern. Rewriting to use `reflect.answer` directly (Hindsight integrated-mode parity) was the bench-Day-3 work that produced the actual R1 number.
- Bench harness `ASTROCYTE_USE_REFLECT` knob + new `ASTROCYTE_REFLECT_SEED_TOP_K` + `ASTROCYTE_REFLECT_MAX_ITERATIONS` env vars — preserved for M21 experiments.
- 4 new routing/discipline blocks in the agentic-reflect system prompt — neutral or slightly helpful in our data; kept since they document the right shape of answer for each Q-type even if the loop doesn't always honor them.

**NOT shipping (deferred to M21):**
- Reflect as default-on path for any consumer (bench or otherwise). Default `agentic_reflect.enabled=False`.
- Per-Q-type pipeline routing (the +5q SHIP candidate from §8.3).
- `done`-tool architectural redesign.
- Trend computation / delta_ops / structured_doc / observations CRUD / mental models CRUD / create_directive MCP (already in original M21 deferred list).

### 8.5 M21 scoping — three concrete directions, pick one

Listed in order of expected lift / cost ratio. M21 cycle doc should evaluate which to commit to.

1. **Per-Q-type pipeline routing in `AstrocyteClient.search()` (best ratio).** Classify the question (cheap LLM call or regex pattern), route temporal → reflect, everything else → recall+answerer. Projected +5q ship per §8.3. ~1 day implementation + 1 bench day. The classifier itself is a known good (M19a per-Q-type routing already classifies for the answerer prompt).

2. **`done`-tool architectural redesign (highest leverage, highest risk).** Force every iteration to produce a candidate answer (not just a tool call), and let the loop commit at any iteration when confidence threshold is met. Or: add an explicit "termination check" tool that the agent MUST call after every 2 tools, returning a Boolean. Tests whether the loop pathology is fixable via tool design rather than prompt content. ~3 day implementation + bench. Could unlock +5 to +12q if successful, could be flat if the underlying model just doesn't reason about convergence.

3. **Pivot away from reflect entirely — pursue Trend + delta_ops + structured_doc (Hindsight parity gap that matters more).** Observations don't evolve in our system; they're write-once at retain time. Hindsight's Trend computation (STABLE/STRENGTHENING/WEAKENING/NEW/STALE) and delta_ops (evidence add/remove/strengthen) are the larger architectural gap and the real differentiator versus a "store-and-recall" system. Original M20 doc deferred these to M21 if reflect was positive — now reflect is NULL, so these become the M21 default priority unless option (1) wins a quick lift first.

### 8.6 Costs + cycle metadata

- Total reflect bench spend: ~$12 across 4 LoCoMo runs (R1/R1b/R1c/R1d) + 1 R0 baseline (LME+LoCoMo parallel).
- Wall time: ~5 hours of bench runs across the day.
- Latency observation: reflect averages ~9-10s/q across all configs at LoCoMo. Per-iteration cost is sub-linear (5 iter ≈ 9s, 10 iter ≈ 9s) — suggests parallel LLM/tool work inside the loop or aggressive caching.
- Tag: v0.15.0 (surface ship), no `bench/m20` label per RELEASING.md convention (semver tag is sufficient — user feedback from M19 cycle close).
