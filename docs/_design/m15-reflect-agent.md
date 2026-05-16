---
title: "M15 — Reflect agent with native tool calling"
draft: false
topic: design
---

# M15 — Reflect agent with native tool calling

**Status:** scoping (2026-05-15)
**Predecessor:** [M14 null verdict](./m13-m14-roadmap.md) — atomic-fact consolidation + entity canonicalization + temporal query analysis did not move the gpt-4o-mini bench at our scale.
**Hypothesis source:** [§12.1 of the M13–M14 roadmap](./m13-m14-roadmap.md#121-reflect-agent-with-native-tool-calling-) — the biggest unrealized lever from the Hindsight gap analysis.

## 1. Locked policy framework

The M14 retrospective showed that policy decisions made mid-experiment leak optimism bias into the result. M15 locks all gates up front; mid-cycle re-scoping is not permitted.

| # | Decision | Locked value |
|---|---|---|
| 1 | Scope | §12.1 alone — reflect agent + 4 tools. NO stacking of §12.2/§12.3 in this cycle. |
| 2 | Answerer model | `gpt-4o-mini` only. Apples-to-apples with M14 baseline. |
| 3 | Budget cap | **$100 hard cap** (LLM + bench costs combined). Cycle halts at $100. |
| 4 | Ship gate | **2σ over 4-run baseline mean**, on the SAME baseline used for M14 close. |
| 5 | DB reset policy | `BENCH_RESET=1` mandatory at every full bench run. M14 lesson — accumulated state inflates latency 30× and skews recall. |
| 6 | Baseline reference | LME top_20 = 63.3% modal / 65.0% mean / ±3.3pp; LoCoMo top_20 = 78.25% ±1.5pp (4-run, post-M14 close). |
| 7 | Replication N | Minimum **3 runs** before any verdict claim. Single-run highs are inadmissible (M14 lesson). |
| 8 | Branch | `feat/m15-reflect-agent`. Squash to main only after ship gate. |
| 9 | Null verdict policy | If 3-run mean fails the 2σ gate, document and revert. No "architectural plumbing" promotion (M14 lesson — that framing diluted the signal). |

### Ship-gate arithmetic

| Bench | Baseline mean | Required improvement (2σ) | Required mean to ship |
|---|---|---|---|
| LME top_20 | 65.0% | +6.6pp | **≥ 71.6%** |
| LoCoMo top_20 | 78.25% | +3.0pp | **≥ 81.25%** |

The LME gate is the primary target; reflect agent's expected lift is "+5-15pp LME" per §12.1. LoCoMo is secondary; the reflect agent doesn't specifically address LoCoMo failure modes.

## 2. Hypothesis

**H1 (primary):** Replacing the one-shot Mem0-harness answerer with an iterative tool-calling agent (max 5 iterations, 4 retrieval tools) raises LME top_20 by ≥ 6.6pp over the 4-run baseline mean.

**Failure modes that would falsify H1:**
- The binding constraint is `gpt-4o-mini`'s reasoning ceiling, not retrieval-completeness. Iterating doesn't help if the model can't compose retrieved evidence.
- Our current flat-candidate retrieval already gets the right facts to the answerer; the bottleneck is downstream of retrieval.
- Tool-calling overhead (latency, prompt tokens) at gpt-4o-mini's small context offsets the iterative-recovery benefit.

**Why this is testable now:** the existing `search()` function in the Mem0 harness adapter is already multi-grain (facts / sections / wikis). M15 doesn't need new retrieval primitives; it needs a new outer loop and 4 tool wrappers around what we already have.

## 3. Architecture

### 3.1 Where the agent loop lives

```
Mem0 harness (vendored)                 Astrocyte (this repo)
─────────────────────                   ──────────────────────
LongMemEval/LoCoMo answerer  ─call──►   astrocyte_client.search(query, user_id)
                                        └── flat candidate list, reranked
                                        (current path — kept as baseline)

NEW PATH (M15):
LongMemEval/LoCoMo answerer  ─call──►   astrocyte_client.reflect_answer(query, user_id)
                                        └── runs LLM agent loop with 4 tools
                                        └── each tool calls back into astrocyte
                                            internals (search_facts, section_recall,
                                            wiki_search, link_expand)
                                        └── returns final answer string
```

**Key design choice:** the reflect agent lives **inside `astrocyte_client.py`** (harness-side), NOT inside `astrocyte` core. The astrocyte core's job is to expose retrieval primitives; the agent loop is a harness-side composition. This keeps the gateway product unaware of bench-specific answerer architecture.

The Mem0 harness's existing answerer is bypassed when `--answerer-mode=reflect` is passed. The harness compares the two modes apples-to-apples — same retain pipeline, same data, different answerer-side composition.

### 3.2 The four tools

Mirror Hindsight's contracts (`engine/reflect/tools.py`) but bind them to our SPI:

| Tool | Hindsight equivalent | Astrocyte binding |
|---|---|---|
| `search_facts(query, top_k)` | `tool_search_observations` | `store.search_facts_semantic(bank_id, qvec, top_k)` — already implemented |
| `search_sections(query, top_k)` | `tool_recall` | `section_recall(store, bank_id, question=query, mode="single-hop", ...)` — already implemented |
| `search_wikis(query, top_k)` | `tool_search_mental_models` | wiki_hits subset of `section_recall` output — already implemented |
| `expand_from(memory_id, depth=1)` | `tool_expand` | **NOT IMPLEMENTED.** `astrocyte_pi_section_links` table exists but no recall-time expansion API. See §3.4. |

### 3.3 Agent loop contract

```python
async def reflect_answer(
    *, query: str, user_id: str, max_iterations: int = 5
) -> str:
    # System prompt: instruct model on tools + answer format
    # Tools: 4 above, exposed as OpenAI function-calling shapes
    # Loop:
    #   for i in range(max_iterations):
    #     resp = await llm.chat(messages, tools=TOOLS)
    #     if resp.tool_calls: execute → append tool results → continue
    #     else: return resp.content
    #   # iteration limit: force-answer with whatever we have
    #   return await llm.chat(messages + [FORCE_ANSWER_PROMPT])
```

Error recovery: tool errors are returned as tool messages with `"error": "..."` and the agent decides whether to retry or pivot. Mirrors Hindsight's pattern.

### 3.4 The `expand_from` tool is the one new thing

Three of four tools wrap existing astrocyte functions. `expand_from` requires a new SPI:

```python
# astrocyte_py/astrocyte/pipeline/link_expansion.py (NEW in M15)
async def expand_from_section(
    *, store, bank_id: str, seed_section_id: str, depth: int = 1,
    max_results: int = 10,
) -> list[SectionHit]:
    """Walk astrocyte_pi_section_links from a seed section.

    Returns sections reachable within `depth` hops, ranked by link weight.
    """
```

This is the smallest piece of new code in M15. If `expand_from` proves valueless mid-cycle (agent never calls it productively), the tool is removed and we proceed with 3 tools. The agent loop and the other 3 tools are not blocked on it.

## 4. Implementation phases

| Phase | Description | Bench gate | Budget |
|---|---|---|---|
| **0** | 4-run baseline re-verification on current HEAD (`6ec61ea` post-M14 close). Confirms the M14-close baseline is still 65.0% / 78.25% on a fresh DB. | None — anchors the gate. | ~$15 |
| **1** | Implement `reflect_answer()` with 3 tools (`search_facts`, `search_sections`, `search_wikis`). No `expand_from` yet. Wire into harness as `--answerer-mode=reflect`. | 3-run smoke on LME-30. **Continue if mean ≥ baseline mean − 1σ (i.e., ≥ 61.7%).** Discontinue if reflect-mode underperforms baseline by > 1σ — that's a regression, not a stepping stone. | ~$25 |
| **2** | Add `expand_from` tool + `link_expansion.py` SPI. Re-bench LME-30 + LoCoMo-200. | 3-run mean. Decide: ship gate met? If yes → Phase 3. If no → null verdict, document, revert. | ~$40 |
| **3** | Final replication: 1 additional LME-30 run + 1 LoCoMo-200 run to confirm Phase 2 mean. Squash to `main`. Update `benchmark-trajectory.mdx`. | 2σ gate over 4-run baseline. | ~$20 |

**Hard halts:**
- Phase 1 regression > 1σ ⇒ **discontinue**. Don't push through; the agentic shape isn't compatible with our retrieval.
- Cumulative spend ≥ $100 ⇒ **halt and document**, regardless of phase.
- Any single LME run < 50% ⇒ **investigate before continuing** (likely a tool bug masquerading as a metric drop).

## 5. Resolved design decisions (was: open questions)

Locked 2026-05-15 before Phase 1 implementation:

1. **Tool result format → STRUCTURED JSON.** Each tool returns `{"results": [{"id": "...", "text": "...", "metadata": {...}}, ...]}`. The `id` field is critical — `expand_from` needs to be able to reference prior results. Context-window pressure mitigated by per-tool `top_k` caps (defaulted at 5; tool description tells the model to use small `top_k` for exploration, larger for final answers).

2. **Tool call budget → 5 iterations max, no per-tool cap.** Mirrors Hindsight. Each iteration may issue one tool call (no parallel tool-call permission in Phase 1 — kept sequential for predictable cost accounting). On iteration 5 with no answer, a final "answer with what you have" prompt is injected; the model must answer.

3. **System prompt → minimal scaffold.** Tool descriptions + output format + "answer concisely from retrieved evidence." NO bank disposition, NO mission, NO directives (all are §12.4/§12.7 and out of scope). Keep the prompt short — gpt-4o-mini context is precious.

4. **Caching → in-memory `(tool_name, args_hash) → result` dict, per-question.** Cleared at start of each `reflect_answer()` call. Prevents the model from re-issuing identical searches across iterations (Hindsight's exact strategy). Cache hits are reported back to the model in the tool result as `{"cache_hit": true, ...}` so it learns to vary queries.

5. **`expand_from` seed selection → model-passed by id.** Tool description explicitly says: "Pass a `section_id` you've seen in a prior tool result." If the model passes an unknown id, the tool returns `{"error": "unknown section_id", "hint": "use ids from search_sections results"}`. Mirrors Hindsight.

**Additional decision (6): error format.** All tool errors return `{"error": "<class>", "message": "<human>", "hint": "<actionable>"}`. The model treats `error` as a signal to pivot, not retry the same call.

## 5b. Phase 0 status — using existing baseline

The locked plan called for a 4-run baseline re-verification on current HEAD. **Skipped** with justification: the existing 4-run baseline was taken at commit `6ec61ea` (post-M14 close). Current HEAD is `d6f0d7a`, which only adds:
- `astrocyte-py/uv.lock` regen (no functional code change)
- `docs/pnpm-lock.yaml` Dependabot devalue 5.7.1 → 5.8.1 (docs site only — does not affect bench runtime)
- `docs/_design/m15-reflect-agent.md` (this file)

None of those changes affect retrieval, extraction, scoring, or the answerer path. The baseline at `6ec61ea` is bench-equivalent to HEAD. Phase 0 budget ($15) released to Phase 2 contingency.

If Phase 1 produces results inconsistent with the existing baseline (e.g., reflect mode at baseline behavior reports < 60% LME), Phase 0 will be re-run as a sanity check before proceeding.

## 6. Out of scope for M15

Documented here so they don't drift in mid-cycle:

- **§12.2 multiplicative score boosts** — separate cycle. Confounds the §12.1 signal.
- **§12.3 link-expansion as a recall strategy** — M15 adds `expand_from` ONLY as a reflect-agent tool, NOT as a fused strategy in `section_recall`. Promoting it to a fused strategy is §12.3.
- **§12.4 disposition + mission injection** — separate cycle.
- **§12.5 gpt-oss-120b retain** — separate cycle (cost lever, not accuracy lever).
- **§12.7 directives** — bench-blocked.
- **Answerer-model upgrades** — locked at gpt-4o-mini.

## 7. Cycle close criteria

The M15 cycle is closed when ONE of:

- **Ship:** Phase 3 4-run mean LME top_20 ≥ 71.6% AND LoCoMo top_20 ≥ baseline − 1σ (i.e., LoCoMo doesn't regress). Squash to main. Update trajectory page.
- **Null verdict:** Phase 2 3-run mean LME top_20 < 71.6%. Document the result in the cycle close section (NEW §8 in this doc). Delete the `feat/m15-reflect-agent` branch. No "architectural plumbing" framing — the code goes.
- **Halt:** $100 budget hit, or Phase 1 regression > 1σ. Document where it halted and why.

## 8. Cycle close — NULL VERDICT (2026-05-16)

### 8.1 Final bench numbers

3-run LME-30 reflect-mode replication on commit `d6f0d7a` (post-M14 close), `BENCH_RESET=1`, gpt-4o-mini answerer + judge, `--user-profile`, `ASTROCYTE_ANSWERER_MODE=reflect`, `ASTROCYTE_REFLECT_MAX_ITER=5`.

| Run | LME top_20 |
|---|---|
| `astrocyte-m15-phase1-run-1` | 60.0% (18/30) |
| `astrocyte-m15-phase1-run-2` | 46.7% (14/30) |
| `astrocyte-m15-phase1-run-3` | 60.0% (18/30) |
| **3-run mean** | **55.6% ±7.7pp** |

**Baseline mean (M14 close): 65.0% ±3.3pp.** Reflect agent regression: **−9.4pp ≈ 2.8σ below baseline**. Phase 1 gate (≥ 61.7%, i.e., baseline − 1σ) missed by **6.1pp**. **Null verdict triggered per locked policy §1.9.**

Per-category baseline 4-run mean vs reflect 3-run mean (LME top_20):

| Category | Baseline 4-run | Reflect 3-run | Δ |
|---|---|---|---|
| knowledge-update | 95.0% | 93.3% | −1.7pp (noise) |
| **single-session-assistant** | 55.0% | **66.7%** | **+11.7pp** ↑ |
| single-session-preference | 20.0% | 13.3% | −6.7pp |
| single-session-user | 80.0% | 66.7% | −13.3pp |
| temporal-reasoning | 75.0% | 60.0% | −15.0pp |
| **multi-session** | 65.0% | **33.3%** | **−31.7pp** ↓↓ |
| **OVERALL** | 65.0% | 55.6% | −9.4pp |

**One real gain, one catastrophic loss, three modest regressions.** The multi-session collapse alone accounts for ~60% of the overall regression.

### 8.2 Why it regressed — the actionable finding

The per-category pattern (§8.1 table) refutes the simple "precision held, synthesis collapsed" story. The actual pattern:

1. **Knowledge-update (95% → 93%):** unchanged. Single-fact precision questions where targeted tool retrieval is competitive with flat retrieval.
2. **Single-session-assistant (55% → 67%, +11.7pp):** **gained meaningfully.** The agent's `search_sections` tool surfaces assistant-speaker turns more cleanly than the flat reranker's blend of user+assistant content. The targeted query "what did the assistant say" is genuinely better-served by a tool call than by broad rerank.
3. **Single-session-user (80% → 67%, −13.3pp):** lost. Inverse of the above — the flat reranker apparently surfaces user-speaker context better than the agent's tool queries.
4. **Temporal-reasoning (75% → 60%, −15pp):** lost. Some temporal cues (relative date expressions in the question) need the broader candidate window the flat path provides.
5. **Single-session-preference (20% → 13%, −6.7pp):** still low (was already a known weak spot from M14). The agent didn't fix it; if anything it slightly worsened.
6. **Multi-session (65% → 33%, −31.7pp):** the catastrophic regression. "How many days did I attend conferences in April?" and similar count/sum questions need the model to gather *all* relevant events before composing. The agent issues 1–2 tool calls each returning `top_k=5`; flat baseline gives 20 reranked candidates — fundamentally broader evidence base for synthesis.

**The actionable signal: per-grain query routing.** The +11.7pp on single-session-assistant is the only genuine gain and suggests that a question's *speaker target* matters for retrieval grain. A simple one-shot **question-type router** (classify the question → pick facts vs sections vs wikis vs flat-union) might capture this gain without paying the multi-session penalty of an iterative agent.

**Hindsight's reflect agent presumably works at production scale because they pair it with §12.2 (multiplicative score boosts) and §12.3 (link-expansion).** Without those, our reflect agent's narrow tool calls have worse recall than the flat baseline's 200-candidate → rerank → 20-answerer path on synthesis-from-breadth categories.

Our minimal reflect agent layered Hindsight's *answerer shape* on top of *M14-tier retrieval primitives*. The bench says the answerer shape isn't the binding constraint — the **per-tool-call retrieval quality** is.

### 8.3 What we learned (worth keeping out of the revert)

1. **Reflect agent shape is viable for precision-class questions.** knowledge-update at 93% mean shows the agent doesn't break precision retrieval. The regression is recall-bound, not reasoning-bound.
2. **gpt-4o-mini is NOT the binding constraint at our retrieval tier.** The model could compose answers fine when its single tool call hit the right page. The bottleneck was tool-call recall completeness.
3. **The cost profile of a tool-calling agent is dominated by tool-call retrieval cost, not LLM iteration cost.** Each agent call took ~30-45 s wall vs ~5 s baseline. Most of the time was waiting on tools; iterations themselves rarely exceeded 2-3.
4. **Per-question memoization across cutoffs (the cache in `ReflectAwareAnswerer`) is essential** — without it, the harness invokes the agent 4× per question for no signal benefit.

### 8.4 Cost accounting

| Phase | Description | Spend (est.) |
|---|---|---|
| Phase 0 | Skipped — used existing M14-close baseline | $0 |
| Phase 1 smoke | LME-6, reflect mode | ~$0.50 |
| Phase 1 run 1 | LME-30, reflect mode | ~$3 |
| Phase 1 run 2 | LME-30, reflect mode | ~$3 |
| Phase 1 run 3 | LME-30, reflect mode | ~$3 |
| **Total** | | **~$10** |

**$10 of $100 cap consumed.** $90 budget reserved for next cycle.

### 8.5 What ships, what reverts

Per locked policy §1.9 ("No 'architectural plumbing' promotion"), all M15 implementation reverts. The doc stays as evidence + recommendation source for the next cycle.

**Reverted (deleted):**
- `astrocyte-py/scripts/mem0_harness/reflect_answerer.py`
- `astrocyte-py/scripts/mem0_harness/reflect_wiring.py`
- M15 monkey-patches in `run_lme.py` and `run_locomo.py`
- `_ASTROCYTE_ANSWERER_MODE_ENV` plumbing in `Makefile`

**Retained:**
- This doc (`docs/_design/m15-reflect-agent.md`) — null-verdict record
- Bench results under `benchmark-results/mem0_harness/lme/astrocyte-m15-phase1-{smoke,run-1,run-2,run-3}/` — evidence

### 8.6 Recommendation for the next cycle

The M15 evidence retargets the next cycle. The Hindsight gap analysis (§12 of M13-M14 roadmap) ordered priorities by **expected bench impact assuming the rest of the stack is also implemented**. Our null verdict + the +11.7pp single-session-assistant gain reorder that:

**Recommended next cycle: M16 — choose between two paths:**

**(A) Question-type router on the baseline answerer.** Capture the +11.7pp single-session-assistant signal by routing questions to the best retrieval grain rather than always running the flat union. Cheap (~2-3 days), small blast radius, directly motivated by M15 evidence. Expected lift: +3-6pp LME if the routing works as predicted.

**(B) Multiplicative score boosts (§12.2) on the baseline flat-candidate answerer.** Answerer-agnostic — improves any top-K retrieval. ~3-5 days. Expected lift: +2-5pp on temporal-heavy categories. Doesn't address multi-session collapse directly but raises the floor for all retrieval.

**Recommendation: (A) first.** It is the smaller, faster, M15-evidence-direct experiment. If it lifts, we have a real ship. If it null-verdicts, we run (B) with the same baseline. (A) and (B) are independent and both useful.

**Path NOT recommended:** more reflect-agent iteration without §12.2 + §12.3 underneath. M15 already showed the narrow-tool-recall failure mode; throwing more iterations or system-prompt tweaks at it won't fix the recall ceiling.

If both (A) and (B) null-verdict, the next probe is **gpt-4o answerer** (§12.8 disambiguating experiment) — that tells us whether the remaining gap to Hindsight's published numbers is genuinely model-bound.

### 8.7 Cycle status

**Closed. Null verdict. Code reverted. Doc retained as evidence.**
