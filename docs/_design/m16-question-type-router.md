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

Phase 1: **harness-side** in `astrocyte-py/scripts/mem0_harness/question_router.py`. Same blast-radius discipline as M15 — astrocyte core untouched; only the bench adapter invokes the router. If Phase 1 ships, promote to `astrocyte/pipeline/question_router.py` in a follow-up commit.

```
Mem0 harness answerer  →  AstrocyteClient.search(query, user_id)
                          ├── classify_question(query) → QuestionType
                          ├── grain_strategy = ROUTING_TABLE[type]
                          └── retrieve_by_strategy(query, user_id, grain_strategy)
                              └── returns flat reranked list (same shape as today)
```

The router changes WHICH retrieval functions get called and HOW their outputs are combined. The downstream contract (flat reranked list to the answerer) is unchanged.

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
| **1** | Implement `question_router.py` + classifier + grain strategies. Wire into `astrocyte_client.search()` behind `ASTROCYTE_QUESTION_ROUTER=1` env flag. Smoke on LME-6. | LME-6 smoke must produce coherent results, no tracebacks, classification distribution looks sane (not all `default`). | ~$5 |
| **2** | 3-run LME-30 + 1-run LoCoMo-200 with router enabled. | If LME 3-run mean ≥ 71.6% AND LoCoMo ≥ 76.7%: Phase 3. If LME any-run < 60%: HALT. Else: replicate vs improve decision. | ~$25 |
| **3** | If Phase 2 borderline: 1 more LME run for 4-run mean. If Phase 2 clears gate: 1 LoCoMo replication. | Final 2σ confirmation. | ~$15 |
| **4** | Promote router from harness to astrocyte core (`astrocyte/pipeline/question_router.py`). Update `AstrocyteClient` to use it by default (no env flag). Squash + push. | None — pure refactor. | ~$0 |

**Hard halts:**
- Any single LME run < 60% in Phase 2 ⇒ HALT (regression > 1σ).
- LoCoMo any-run < 75% in Phase 2 ⇒ HALT (LoCoMo regression).
- Cumulative spend ≥ $50 ⇒ HALT, document.

## 5. Open design decisions (resolve before Phase 1 code)

1. **Classification caching scope.** Within a single bench run, the same question text never repeats (LME has distinct questions per user). But within an `AstrocyteClient.search()` call from per-cutoff iteration, the same `(user_id, question)` may classify multiple times. Cache per-call.

2. **Multi-type questions.** Some questions are both temporal AND factual. Classifier returns one type. Mitigations:
   - (a) Accept the single-type call and trust the classifier's primary signal.
   - (b) Let classifier return a list with a primary; build a merge strategy.
   - **Tentative answer: (a) for Phase 1.** Simplest; if Phase 1 borderline-passes, (b) becomes a Phase 2 lever.

3. **Default strategy when classifier fails (timeout, error).** Always `default` (flat-union) — degrades to baseline behavior. Never silently break the bench.

4. **Empty results from a non-default strategy.** If `factual` route returns 0 facts (no fact-grain hits), fall back to `default` for THIS question. Avoids the trap where a misclassified question gets nothing back.

5. **LoCoMo answer questions don't have categories like LME's.** Classifier doesn't need to know about benches; it operates on raw question text. The 6-class taxonomy applies regardless.

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
