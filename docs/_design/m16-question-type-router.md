---
title: "M16 — Question-type router for retrieval grain selection"
draft: false
topic: design
---

# M16 — Question-type router for retrieval grain selection

**Status:** scoping (2026-05-16)
**Predecessor:** [M15 null verdict](./m15-reflect-agent.md) — reflect agent regressed overall (−9.4pp LME) but produced one real signal: **single-session-assistant gained +11.7pp**, suggesting per-grain query routing is a viable lever absent the iterative agent overhead.
**Hypothesis source:** M15 §8.2 — different question shapes have different optimal retrieval grains. Always running the flat union sacrifices the gain that a grain-targeted query would capture for some question classes.

## 1. Locked policy framework

| # | Decision | Locked value |
|---|---|---|
| 1 | Scope | Question-type router ONLY. No score-boost tuning, no agent changes, no new SPI beyond the router module. |
| 2 | Answerer model | `gpt-4o-mini`. Apples-to-apples with M14 baseline + M15 reflect runs. |
| 3 | Budget cap | **$50 hard cap** (LLM classification + bench costs). Tighter than M15's $100 — smaller scope, deserves smaller envelope. |
| 4 | Ship gate | **2σ over 4-run baseline mean.** LME ≥ 71.6%; LoCoMo must not regress by > 1σ (≥ 76.7%). |
| 5 | Halt gate | Any single LME run < 60% (i.e., > 1σ below baseline mean) ⇒ stop Phase 1 immediately. Don't push through. |
| 6 | DB reset policy | `BENCH_RESET=1` mandatory at every full bench run. |
| 7 | Baseline reference | LME top_20 = 65.0% ±3.3pp; LoCoMo top_20 = 78.25% ±1.5pp (post-M14 close, 4-run). |
| 8 | Replication N | 3 runs minimum to claim a verdict. 4th run only if 3-run mean is within 0.5σ of the ship gate. |
| 9 | Branch | `feat/m16-question-router`. Squash to main only after ship gate. |
| 10 | Null verdict policy | If 3-run mean fails the 2σ gate, revert and document. No "architectural plumbing" promotion. |
| 11 | Genericity constraint | Question-type taxonomy must be **abstract retrieval-strategy classes**, NOT LME-specific categories. Must apply to LoCoMo, LME, and future benches. |

### Ship-gate arithmetic

| Bench | Baseline mean | Required improvement | Required mean to ship |
|---|---|---|---|
| LME top_20 | 65.0% | +6.6pp (2σ) | **≥ 71.6%** |
| LoCoMo top_20 | 78.25% | non-regression > 1σ | **≥ 76.7%** (i.e., no worse than baseline − 1σ) |

LoCoMo's gate is asymmetric: routing should not *hurt* LoCoMo even if it doesn't help. M15 showed the danger of a change that helps one bench while sinking another.

## 2. Hypothesis

**H1 (primary):** A question-type router that selects the best retrieval grain per question (rather than always returning the flat union of facts + sections + wikis) lifts LME top_20 by ≥ 6.6pp over the 4-run baseline mean, without regressing LoCoMo top_20 by > 1.5pp.

**Why this is testable:** M15 produced a clean +11.7pp gain on single-session-assistant when the agent's targeted tool query for sections outperformed the flat union. If that signal generalizes — i.e., other question classes also have a best-grain that the flat union dilutes — a one-shot router captures the gain without paying the agent's iteration cost or the multi-session recall penalty.

**Failure modes that would falsify H1:**
- The +11.7pp was a single-category artifact and doesn't generalize. Other classes' "best grain" is in fact the flat union.
- The classifier itself is noisy enough at gpt-4o-mini to swap correctly-routed wins for misrouted losses.
- The flat union's value is precisely the *averaging* across grains — single-grain routing surfaces grain-specific failures that the union masked.

## 3. Architecture

### 3.1 Where the router lives

**Framework code (`astrocyte/pipeline/question_router.py`) from day 1, behind a feature flag with default OFF.** Not harness-side.

This is a correction from M15's pattern. M15's reflect agent and the original M16 draft both put the experiment harness-side with a "promote to core in Phase 4" plan. That pattern has three failure modes we want to avoid:

1. **Bench measures something users will never run.** Production calls `AstrocyteClient.search()` (or the equivalent recall entry point). If the router only exists in the harness, the bench number reflects a code path no user will ever execute. The bench loses its value as a production indicator.
2. **"Promote in Phase 4" rarely happens.** Once attention moves on, harness-only features become indefinite technical debt. Six months later, ripping out the harness implementation to re-build in core is a chore nobody volunteers for.
3. **The harness can take API shortcuts that production can't.** Reaching into `client._bank_for(...)`, `client._doc_ids`, `client._md_text_by_user` private state works in-process. A real client (Cerebro calling Astrocyte over HTTP/gRPC) can't. By implementing in harness we postpone facing the API-design constraint.

Concretely:

- **`astrocyte/pipeline/question_router.py`** — generic, framework-level. Takes `RetrievalContext(store, embedding_provider, bank_id, document_id, md_text)` as an explicit argument. No private-attribute access. No bench-specific shortcuts. Any caller (Cerebro, future apps, the Mem0 adapter) constructs the context the same way.
- **`AstrocyteClient`** — the Mem0 adapter. Adds an `enable_question_router: bool = False` constructor flag. When True, `search()` builds a `RetrievalContext` and dispatches via the core router; when False, the baseline path is unchanged.
- **`run_lme.py` / `run_locomo.py`** — read `ASTROCYTE_QUESTION_ROUTER=1` env var, pass it to the client subclass at construction. No monkey-patching, no module-level injection.

Ship = flip the default. Revert = remove the module + flag. There is no "promote to core" follow-up — the code already lives there.

```
Mem0 harness answerer
  → AstrocyteClient.search(query, user_id)
    ├─ if not enable_question_router: baseline path (unchanged)
    └─ else: ctx = RetrievalContext(...)
              cls = classify_question(query, openai_client)
              if cls.confidence < 0.6: route to "default"
              results = retrieve_by_question_type(ctx, type, query, top_k)
              (cross_encoder_rerank applied inside)
              returns flat list with same shape as baseline
```

Production callers (e.g., Cerebro, future apps) can use the router directly without going through the Mem0 adapter — `astrocyte.pipeline.question_router.route_and_retrieve(...)` is a public composable.

### 3.2 Question-type taxonomy (generic across benches)

Per locked policy §11, types must be retrieval-strategy classes, not bench-category labels.

| Type | Detection cue | Optimal grain hypothesis |
|---|---|---|
| `factual` | "what is", "what was", "when did", asks for a single attribute | facts (precise, atomic) |
| `narrative` | "what did X say", "tell me about", asks for conversational content | sections (preserves phrasing) |
| `aggregative` | "how many", "how often", "across all", count/sum/list | flat-union (breadth wins) |
| `preferential` | "suggest", "recommend", "what should I", taste-aware | wikis + sections |
| `temporal` | explicit date band ("in April", "after my service"), relative time | facts with temporal filter |
| `default` | classifier low-confidence or unmatched | flat-union (safe fallback) |

This taxonomy maps onto LME's 6 categories and LoCoMo's 5 categories but is itself bench-agnostic.

### 3.3 Classifier shape

**Phase 1 classifier: LLM-based** with `gpt-4o-mini` and a structured output (Pydantic shape).

```python
class QuestionClassification(BaseModel):
    question_type: Literal["factual", "narrative", "aggregative",
                           "preferential", "temporal", "default"]
    confidence: float  # 0.0–1.0
```

Prompt: ~50 tokens system + the query. Per-question cost: ~$0.0001. For LME-30: ~$0.003 per run. Negligible.

**Why LLM over regex:** regex misses paraphrases and would require bench-specific tuning. The taxonomy is small (6 classes) and the classifier output is structured — gpt-4o-mini handles this trivially.

**Confidence threshold:** if `confidence < 0.6`, fall back to `default` (flat-union). Prevents low-confidence classifications from causing routing damage.

### 3.4 Grain-retrieval strategies

| Type | Strategy |
|---|---|
| `factual` | `search_facts_semantic(top_k=20)` → return reranked top_K |
| `narrative` | `section_recall(mode="single-hop")` → reranked sections only |
| `aggregative` | current flat-union (facts + sections + wikis) — no change from baseline |
| `preferential` | `section_recall(wiki_enabled=True, wiki_top_k=8)` + `search_facts_semantic(top_k=10)` |
| `temporal` | `section_recall(mode="temporal", ...)` + fact search filtered by extracted date band |
| `default` | current flat-union (baseline behavior) |

Each strategy returns a flat reranked list. The answerer never sees the grain — same downstream contract.

## 4. Implementation phases

| Phase | Description | Bench gate | Budget |
|---|---|---|---|
| **0** | Skip — baseline (4-run M14 close) is current HEAD; no functional code change since. | None. | $0 |
| **1** | Implement `astrocyte/pipeline/question_router.py` (framework-side) + classifier + 6 grain strategies. Add `enable_question_router=False` flag to `AstrocyteClient`. Runners read `ASTROCYTE_QUESTION_ROUTER=1` env var and pass through to client. Smoke on LME-6. | LME-6 smoke must produce coherent results, no tracebacks, classification distribution looks sane (not all `default`). | ~$5 |
| **2** | 3-run LME-30 + 1-run LoCoMo-200 with router enabled. | If LME 3-run mean ≥ 71.6% AND LoCoMo ≥ 76.7%: Phase 3. If LME any-run < 60%: HALT. Else: replicate vs improve decision. | ~$25 |
| **3** | If Phase 2 borderline: 1 more LME run for 4-run mean. If Phase 2 clears gate: 1 LoCoMo replication. | Final 2σ confirmation. | ~$15 |
| **4** | Flip the default: change `enable_question_router: bool = False` to `True` in `AstrocyteClient` constructor (and any production caller default). Squash + push. | None — single-line default change; router code already in framework. | ~$0 |

**Hard halts:**
- Any single LME run < 60% in Phase 2 ⇒ HALT (regression > 1σ).
- LoCoMo any-run < 75% in Phase 2 ⇒ HALT (LoCoMo regression).
- Cumulative spend ≥ $50 ⇒ HALT, document.

## 5. Resolved design decisions (was: open questions)

Locked 2026-05-16 before Phase 1 implementation:

1. **Classification caching → per-`AstrocyteClient.search()`-call cache, in-memory.** The same `(user_id, question)` classifies once and reuses across the per-cutoff loop. Per-question cost: one ~$0.0001 classify + reused thereafter.

2. **Multi-type questions → SINGLE PRIMARY.** Classifier returns ONE `question_type`. Rationale: Hindsight's own design has no static classifier (their analog is the reflect agent's iterative tool calls, which we already null-verdicted in M15). Engineering grounds: single-primary is simpler, smaller blast radius, easier to debug. If M16 borderline-passes, ranked-list becomes a hypothetical M17 lever; per the locked policy (§4), borderline misses null-verdict immediately.

3. **Classifier failure → `default` (flat-union).** Timeouts, parse errors, missing-API-key all route to baseline behavior. Never silently break the bench. Logged at WARNING.

4. **Empty results from a non-default strategy → fall back to `default` for THIS question.** A `factual` route that returns 0 facts is a misclassification or a question whose answer isn't fact-grained; instead of returning nothing, run the flat-union for that one question. Prevents the trap where misclassification = no evidence reaches the answerer.

5. **Genericity (per locked policy §11).** Classifier operates on raw question text. The 6-class taxonomy is bench-agnostic — applies to LoCoMo, LME, and future benches without modification.

6. **Additional locked decision: confidence threshold = 0.6 strict.** Classifier outputs below 0.6 confidence route to `default`. Conservative — reduces classification-noise-driven regressions. Hindsight's reflect agent has no equivalent constant; this is our engineering choice.

## 6. Out of scope for M16

- **§12.2 multiplicative score boosts** — separate cycle (the "B" alternative).
- **§12.3 link-expansion** — separate cycle.
- **Answerer-model upgrades** — locked at gpt-4o-mini.
- **Retain-pipeline changes** — none. M16 is recall-side only.
- **Iterative agents** — M15 already null-verdicted.

## 7. Cycle close criteria

The M16 cycle is closed when ONE of:

- **Ship:** Phase 3 3-or-4-run mean LME top_20 ≥ 71.6% AND LoCoMo top_20 ≥ 76.7%. Promote router to core (Phase 4). Squash to main. Update trajectory page.
- **Null verdict:** Phase 2/3 fails the gate. Document the result here (§8). Delete `feat/m16-question-router`. No "architectural plumbing" promotion.
- **Halt:** Any halt condition tripped. Document where and why.

## 8. Cycle close (TBD)

To be filled in at end of cycle with: final bench numbers, per-category and per-classification-type breakdown, total spend, whether shipped/null/halted, and (if null) what the diagnostic data suggests for the next cycle.
