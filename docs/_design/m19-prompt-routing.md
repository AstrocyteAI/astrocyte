# M19 — Per-question-type prompt routing + Hindsight `create_directive`

**Status:** scoping (2026-05-18)
**Inputs:** [`m18-quick-wins.md`](m18-quick-wins.md) §8 cycle close, M18b + M18b.2 ablation data
**Predecessor:** M18b shipped B1-dp+RRF (LoCoMo 83.75% 2-run mean, +1.75pp above M17+1σ gate)

---

## 1. Why this cycle exists

M18b + M18b.2 tested **four retrieval-side features** (B2 directive_compile, B3 spreading_activation, B4 episodic_extract) plus their stacking combinations as candidates to add to the shipped RRF-fused fact_recall. **None composed.** Either individually null (B3/B4) or actively anti-composing when stacked (B5 = B1+prompt; B1+B3). Pattern: every stacking attempt produced a per-category trade (one category +N / another category −N) that net out to combined ≤ best individual.

The conclusion: **the RRF fusion has a saturation point.** Adding more parallel siblings dilutes cross-strategy agreement rather than reinforcing it. Further accuracy work must target **new pipeline stages**, not more RRF siblings.

Meanwhile, the strongest single feature we measured in M18b — **prompt-only (Hindsight SSP block, +9.5q combined vs B0)** — anti-composes with shipped B1-dp+RRF on multi-hop synthesis (B5 stacking confirmed 2/2 runs). We have a known +9.5q lift sitting on the shelf because we can't ship it without losing the +8q B1-dp+RRF already delivers.

**M19 is the cycle that fixes that.**

## 2. Two parallel workstreams

### Workstream A — Per-question-type prompt routing (primary)

**Goal:** ship the Hindsight SSP prompt's +9.5q lift WITHOUT losing B1-dp+RRF's +8q lift.

**Mechanism:** apply the Hindsight SSP block ONLY to questions where it helps (recommendation / preference / open-domain) and NOT to questions where it hurts (multi-hop synthesis).

**Implementation paths (ranked by cost):**

1. **Classifier-then-prompt-template-selection** (~1 week) — small classifier (regex or LLM-judge) tags each question as one of {recommendation, multi-hop, single-hop, temporal, other}. Routing table selects the right system-prompt block per tag. Reuses the M16 question-type router design (see [`m16-question-type-router.md`](m16-question-type-router.md)).

2. **Universal prompt with conditional blocks** (~2-3 days) — single system prompt with `IF this question is about preferences THEN ...` style sections. Cheaper but more brittle.

3. **Per-cutoff-K prompt variants** (~3 days) — different prompts for top_10 vs top_20 vs top_50 (different K = different recall density = different reasoning needs). Probably orthogonal to (1).

**Validation:**
- Repeat B5-stack (B1-dp+RRF + prompt) with router enabled
- Expected: LoCoMo 84-86% (matches sum of individual lifts since categories no longer compete)
- 2-run mean must clear current shipped baseline (189/230) by ≥3q to ship

**Risk:** the question classifier itself adds latency + cost. Budget: ≤ +100ms per query, ≤ +$0.001 per query.

### Workstream B — Hindsight `create_directive` MCP tool

**Goal:** replace M18b's broken auto-compile B2 with the architecturally correct user-authored directive path.

**Background:** Hindsight's `mental_models.subtype='directive'` rows are USER-AUTHORED hard rules installed via the `create_directive` MCP tool ([`hindsight-api-slim/hindsight_api/mcp_tools.py:_register_create_directive`](../../hindsight/hindsight-api-slim/hindsight_api/mcp_tools.py)). Our M18b directive_compile was an LLM-pass that auto-extracted from preferences — wrong architecture, regressed SSP.

**Implementation (~1 week):**

1. Add `Astrocyte.create_directive(bank_id, title, content, scope=...)` core method writing `MentalModel(kind='directive', subtype='user-authored')`
2. Add MCP tool `create_directive` in `astrocyte-mcp` server with the Hindsight tool signature
3. Update `get_user_profile` to surface `kind='directive'` user-authored rows (already does for the auto-compiled ones; just stop auto-compiling)
4. Keep `astrocyte/pipeline/directive_compile.py` in-tree but remove the auto-trigger from `run_document_postprocess`; module remains as a reference/possible future hybrid
5. Mark `DirectiveCompileConfig.enabled` as a NO-OP / DEPRECATED with a runtime warning

**Validation:** not a bench-driven feature; success criterion is API surface + Synapse integration test. No expected LME/LoCoMo lift.

**Why now:** unblocks future Synapse/MCP integration where the agent installs directives based on user feedback. Removes architectural debt.

## 3. Secondary candidates (deferred to M20)

| Candidate | Effort | Rationale | Why not M19 |
|---|---|---|---|
| **BM25 as pre-RRF gating filter** | ~3 days | NEW stage (not parallel sibling) — gate the RRF input pool with sparse keyword match before fusion | Awaits Workstream A: if prompt routing alone reaches 85%+, BM25 may be unnecessary |
| **Reflect agent revisit (M15 null)** | ~1 week | Iterative recall + reflect could compose with sharper RRF fusion now | M15 null verdict still recent; lower priority than routing |
| **Document Engine bench** | ~1 week (FinanceBench setup + 2 runs) | Validates M17 engine split on multi-document QA | Different bench/dataset; not blocking M19 accuracy work |
| **Bench harness hardening** | ~2 days | Fix idempotency-marker resume footgun (we hit it 2× in M18b); pin `--extra bench --extra rerank` in Makefile | Operational improvement, no accuracy impact |

## 4. Bench plan

| Run | Config | Purpose | Ship gate |
|---|---|---|---|
| **R0** | Currently shipped B1-dp+RRF baseline | parity sanity check | 2-run mean ≥ 188/230 |
| **R1** | B1-dp+RRF + prompt + question router | primary M19 ship candidate | 2-run mean ≥ 192/230 (clears B1-dp+RRF by +3q) |
| **R2** | R1 + per-cutoff-K variants (Workstream A.3) | secondary lift | optional |
| **R3** | R1 + BM25 pre-RRF filter | M20 preview | optional |

Total bench budget for M19: ~$15-20.

## 5. Risk + mitigations

**Risk: question classifier introduces a new failure mode.** A misclassified question gets the wrong prompt → worse answer than B0 baseline.
- Mitigation: classifier MUST have a "default" tag for questions it can't confidently route; default = current shipped prompt (no Hindsight block); only tag with high confidence.

**Risk: prompt routing complicates the bench methodology.** Currently we modify ONE upstream prompt; routing means modifying multiple.
- Mitigation: document the routing table prominently in CHANGELOG and bench-results metadata; publish per-tag breakdown so any disclosure shows the actual mix.

**Risk: Workstream B (create_directive) lacks a clean accuracy signal.** Without a bench, it's hard to know if it "works."
- Mitigation: integration test against Synapse (or a mock MCP client) is the success criterion. Track adoption (do users actually call it?) as the in-production metric.

## 6. Sequence

**Week 1:** Workstream A.1 (classifier + routing) — implement + unit tests
**Week 2:** Workstream A bench validation (R0 + R1, 2 runs each)
**Week 3:** Workstream B (create_directive MCP tool) — implement + integration test against Synapse mock
**Week 4 (optional):** A.3 (per-cutoff-K variants) + R2 bench if A is shipping cleanly

**Hard gate:** if Workstream A R1 doesn't clear ship gate (+3q over shipped baseline) by end of week 2, halt and revisit M19 scope — the anti-composition problem may need a deeper architectural fix.

## 7. Out of scope

- Reflect agent revisit (deferred to M20)
- Document Engine FinanceBench (separate workstream)
- Production deployment story (M21+)
- MIP integration with new fact_recall (M20+)
