# M19 — Per-question-type prompt routing + Hindsight `create_directive`

**Status:** scoping (2026-05-18)
**Inputs:** [`m18-quick-wins.md`](m18-quick-wins.md) §8 cycle close, M18b + M18b.2 ablation data
**Predecessor:** M18b shipped B1-dp+RRF (LoCoMo 83.75% 2-run mean, +1.75pp above M17+1σ gate)

---

## 1. Why this cycle exists

M18b + M18b.2 + the all-ON B5 sanity check tested every plausible retrieval-side stack. The picture is sharper than initially framed:

- **Single features cluster at 187-192/230** combined accuracy (B1-dp+RRF, prompt-only, B3, even all-ON-B5 all hit this band)
- **2-feature stacks ANTI-COMPOSE to 182-187** (B5=B1+prompt and B1+B3 both fall below their best individual constituent — each strong signal displaces the other's category wins)
- **5-feature all-ON returns to 189/230** with **zero per-category LoCoMo regression** (open-domain +5, multi-hop +1, temporal +2) — at high feature count no single signal dominates and the system falls back to baseline+coverage

**The 189-192/230 ceiling is hit by multiple configurations.** Adding/removing RRF siblings doesn't break through. The fix is a NEW composition mechanism that lets signals modulate the final ranking rather than competing for rerank slots.

**Hindsight already solves this** via bounded multiplicative boosts in [`hindsight-api-slim/hindsight_api/engine/search/reranking.py`](../../hindsight/hindsight-api-slim/hindsight_api/engine/search/reranking.py):

```python
combined = CE_normalized × recency_boost × temporal_boost × proof_count_boost
# each boost ∈ [1−α/2, 1+α/2], so max combined ∈ [~0.81, ~1.21]
```

**Astrocyte already has the primitive** — [`astrocyte/pipeline/rerank_boosts.py`](../../astrocyte-py/astrocyte/pipeline/rerank_boosts.py) implements the same multiplicative pattern (recency × temporal-band × proof-count) and is wired into `section_rerank.py:141`. But the bench's unified fact+section rerank pool (`astrocyte_client.py:984`) calls `cross_encoder_rerank` directly, **bypassing the boost layer**. That's the gap.

**M19 is the cycle that wires multiplicative boosts into the unified rerank, then re-tests stacking.**

## 2. Three workstreams (revised priority post all-ON B5)

### Workstream C — Multiplicative bounded boosts on unified rerank (PRIMARY, ~1-2 days)

**Goal:** wire the existing `rerank_boosts.apply_boosts` into the bench's unified fact+section rerank pool so retrieval-side signals modulate the final ranking rather than competing for rerank slots.

**Why this is now PRIMARY** (was tertiary in original M19 scope): all-ON B5 confirmed the ceiling is reached at 189-192/230 by multiple configurations and 2-feature stacks anti-compose. The Hindsight pattern (multiplicative bounded composition) is the architecturally identified fix, and we already implement it for sections — just not for the unified pool.

**Implementation:**

1. Locate the unified rerank call site: `scripts/mem0_harness/astrocyte_client.py:984-994` calls `cross_encoder_rerank(items, query)` directly.
2. After the CE rerank, apply `rerank_boosts.apply_boosts(items, sections_by_key, query_range=date_range, proof_counts=...)` with the existing `RerankBoostConfig(enabled=True)` defaults.
3. Need to compute / pass:
   - `sections_by_key`: lookup `(document_id, line_num) → PageIndexSection` for date fields
   - `proof_counts`: number of facts linked to each section (cheap COUNT per section, optional)
   - `query_range`: already computed by question_annotator (M14 path) or from `date_range` in the bench's recall analysis
4. Run the same M18b B5-stack and B1+B3 stack benches with boosts ON to see if anti-composition is fixed.

**Hypothesis:** the 2-feature stack anti-composition disappears because:
- B1-dp+RRF adds temporal candidates → they don't displace multi-hop ones; instead the temporal_band boost modulates them up only when relevant
- Prompt block influences the answerer side; bounded boosts on the candidate side give it bounded influence on what reaches it
- Stacks like B5 (B1+prompt) and B1+B3 should compose without per-category trade-offs

**Validation:** R-boost-stack runs (B5-stack + boosts; B1+B3 stack + boosts).

**Risk:** the `apply_boosts` API needs `PageIndexSection` lookup; if not all hits have section anchors (post-MemoryFact rename allows nullable), the proof-count and date paths need to handle `None` gracefully.

### Workstream A — Per-question-type prompt routing (secondary, ~1-2 weeks)

**Goal:** ship the Hindsight SSP prompt's +9.5q lift WITHOUT losing B1-dp+RRF's +8q lift.

**Why secondary now:** if Workstream C fixes the stacking anti-composition, prompt-only may compose with B1-dp+RRF directly (since both signals are bounded by the new boost layer). Workstream A becomes a refinement, not a critical fix.

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

**Phase 1 — Workstream C primary validation (~$8, ~2 hours):**

| Run | Config | Purpose | Decision |
|---|---|---|---|
| **C0** | Currently shipped B1-dp+RRF + apply_boosts wired into unified rerank | parity check + boosts overhead measurement | 2-run mean ≥ 187/230 (no regression) |
| **C1** | C0 + Hindsight SSP prompt (the original B5-stack with boosts) | does multiplicative composition fix the anti-comp? | 2-run mean ≥ 192/230 → SHIP boosts + prompt |
| **C2** | C0 + spreading_activation (the B1+B3 stack with boosts) | does the temporal/multi-hop trade resolve? | 2-run mean > B3 single (185.5) → ship B3 + boosts |

**Phase 2 — Conditional on C1 result:**
- If C1 ships: M19 headline is ~192-195/230 (multiplicative boosts + prompt combined). Workstream A becomes a refinement, not necessary for the headline.
- If C1 doesn't ship: revert to original M19 plan — implement Workstream A (per-question-type prompt routing) to ship the prompt lift directly.

**Phase 3 — Workstream B (Hindsight `create_directive` MCP tool):** runs in parallel with Phase 1/2; no bench dependency.

Total bench budget for M19: ~$15-20 (Phase 1+2).

## 5. Risk + mitigations

**Risk: question classifier introduces a new failure mode.** A misclassified question gets the wrong prompt → worse answer than B0 baseline.
- Mitigation: classifier MUST have a "default" tag for questions it can't confidently route; default = current shipped prompt (no Hindsight block); only tag with high confidence.

**Risk: prompt routing complicates the bench methodology.** Currently we modify ONE upstream prompt; routing means modifying multiple.
- Mitigation: document the routing table prominently in CHANGELOG and bench-results metadata; publish per-tag breakdown so any disclosure shows the actual mix.

**Risk: Workstream B (create_directive) lacks a clean accuracy signal.** Without a bench, it's hard to know if it "works."
- Mitigation: integration test against Synapse (or a mock MCP client) is the success criterion. Track adoption (do users actually call it?) as the in-production metric.

## 6. Sequence

**Week 1 (Workstream C):**
- Day 1: Wire `apply_boosts` into `astrocyte_client.py` unified rerank (~half day code + tests)
- Day 2: Compute `sections_by_key` + `proof_counts` helpers; handle nullable section anchors
- Day 3-4: Run Phase 1 bench (C0 + C1 + C2, 2 runs each)
- **Hard gate:** if C1 doesn't clear 192/230 by end of week, fall back to Workstream A path

**Week 2:**
- If C1 shipped: write up + ship M19 release (LoCoMo headline)
- If C1 didn't ship: implement Workstream A.1 (classifier + routing)
- Begin Workstream B (create_directive MCP tool) in parallel

**Week 3:**
- Workstream B integration test against Synapse mock
- Workstream A bench (R0 + R1) if needed

**Week 4 (optional refinements):**
- Per-cutoff-K prompt variants (Workstream A.3)
- BM25 pre-RRF gating (M20 preview)

**Hard gate (revised):** if Workstream C C1 doesn't clear 192/230 by end of week 1, fall back to Workstream A (per-question-type prompt routing). If neither C nor A clear 192/230 by end of week 2, halt and revisit M19 scope — the anti-composition problem may need a deeper architectural fix (e.g., separate the section-rerank pool from the fact-rerank pool entirely).

## 7. Out of scope

- Reflect agent revisit (deferred to M20)
- Document Engine FinanceBench (separate workstream)
- Production deployment story (M21+)
- MIP integration with new fact_recall (M20+)


## 8. Cycle close — SHIPPED as M19a (2026-05-18, v0.14.0)

### 8.1 What actually shipped

The original 3-workstream plan changed mid-cycle based on bench evidence. Final ship config:

| Workstream | Original plan | Actual outcome |
|---|---|---|
| **A** — Per-Q-type prompt routing | implement classifier + per-tag routing table | shipped as **4 inline category blocks in single answerer prompt** (Hindsight pattern — no external classifier needed). Simpler than planned. |
| **B** — `create_directive` MCP tool | implement Hindsight user-authored directive path | **NOT shipped** — deprecated old auto-compile path instead; MCP tool deferred to M20+ |
| **C** — Multiplicative bounded boosts on unified rerank | wire `apply_boosts` into bench's unified pool | implemented + benched in two iterations (C0, C0' with date plumbing); both regressed (-7q vs shipped). **Removed.** Hindsight default alphas (recency=0.2) mismatch our temporal-reasoning workload. |
| **NEW: Enriched fact extraction (Hindsight `why` parity)** | not in original plan | added mid-cycle after C regression diagnosis. Made the M19a composition work — see §8.3. |

### 8.2 Bench results (2-run mean, top_20)

| Run | LME | LoCoMo | Combined/230 | Decision |
|---|---|---|---|---|
| B0 baseline | 66.67% | 80.5% | 78.70% | — |
| B1-dp+RRF (M18b shipped) 2-run mean | 71.67% | 83.75% | 82.17% | already shipped |
| prompt-only (M18b validated, deferred) 2-run mean | 78.33% | 83.50% | 82.83% | anti-composed with B1 → deferred |
| **M19a 2-run mean (SHIPPED)** | **81.67%** | **84.25%** | **193 (83.91%)** | ✅ ship |
| (worst regression run) M19-C0' (boosts on, dates plumbed) | 66.67% | 81.0% | 182 (79.13%) | regression confirmed; removed |

Cleared M17+1σ LoCoMo ship gate (82.0%) by **+2.25pp**. +4q over previously-shipped B1-dp+RRF mean, +12q over B0.

### 8.3 Why the composition finally worked

Prior M18b stacking attempts (B5 = B1+prompt; B1+B3) all showed per-category anti-composition: gains in one category cancelled losses in another, net combined ≤ best individual. M19a composed cleanly because:

1. **The prompt block became routing-aware** — the multi-hop / temporal / KU inline blocks explicitly prohibit SSP framing. The LLM applies the SSP block only on recommendation questions. The bleed that caused B5-stack's multi-hop crash is gone.
2. **Enriched extraction gave the SSP prompt something to work with** — preference facts now carry strength + scope + reason in their text. M19a r2 hit LME SSP 5/5 ceiling (first time across all M-cycles).
3. **directive_compile's negative effect was removed** — the auto-compressed directives that bled across categories are gone; deprecation warning fires if someone re-enables.

The retrieval-feature ceiling identified in M18b §8.0b finding #6 still holds (no additional RRF sibling composes with B1-dp+RRF), but the breaker is **answer-time prompt routing + retain-time extraction richness**, not more recall. This sharpens the M20+ priority: answer-side mechanisms over retrieval-side additions.

### 8.4 Lessons banked

1. **`apply_boosts` regresses on workloads where "older = often more relevant."** Hindsight default alphas (recency=0.2, temporal=0.2) are tuned for their dataset; LME's temporal-reasoning and LoCoMo's multi-hop questions need older sessions for date arithmetic and cross-session synthesis. The boost layer stays available (used in `section_rerank`) but the unified-pool wiring was removed.

2. **Per-Q-type routing doesn't need a classifier — inline category blocks in the prompt are enough.** Hindsight does this; we copied the pattern. The LLM routes itself based on question shape. Saved ~1-2 weeks of classifier work the original plan budgeted.

3. **Retain-time richness is the durable lever.** The enriched extraction (Hindsight `why` parity) was the smallest code change (~20 lines of prompt update) but unlocked the biggest single-run LME (86.67%, r2). The extracted text is the substrate everything else depends on; spending tokens at extract-time pays back at every recall.

4. **Code that doesn't ship still earns its keep when properly removed.** Boost-layer wiring removal was clean because we'd written it as a guarded try/except. Code in `_hindsight_prompt.py` and `compute_boost_multiplier` stays as primitives — proven valuable for `section_rerank` and future Workstream A iterations.

### 8.5 Pending (deferred to M20+)

- `create_directive` MCP tool (Workstream B) — user-authored directives via Hindsight pattern; enables Synapse integration
- Reflect agent revisit (M15 null verdict) — iterative recall now that recall path is sharper
- Document Engine bench on FinanceBench / DoubleBench (untested at scale)
- Bench harness hardening (idempotency-marker resume footgun; `uv sync --extra X` removes other extras)
- Tune `rerank_boosts` alphas for our workload (lower recency_alpha, higher temporal_alpha) — if worth pursuing
