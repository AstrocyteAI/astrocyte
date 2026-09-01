# M36 — Reflect / iterative reasoning loop

**Status**: CLOSED — executed as bench cycle `v015o` (2026-05-23), **gate FAILED** (62.2% LME vs ≥80% criterion); hybrid routing ships default-OFF. Cycle close reconstructed 2026-09-01 — see §Cycle close at the bottom.
**Cycle**: v0.15.0 (per user: "I insist on solving these now")
**Ship-gate**: LME top_20 (or max_tokens_2048) ≥ 80%

## Problem statement

Across 10+ bench cycles we have ceiling-bounded LME at ~74%. Every change
in the **retrieval layer** (channels, weights, segmentation, reranker,
cutoffs, token budget) shuffles ±3-5 questions across categories without
breaking through.

Pattern: **LME tests cross-session reasoning, our architecture does
cross-session retrieval and hopes the answerer reasons in one shot.**

Concrete examples of where single-shot reasoning fails:

| LME question | What's needed | What we do |
|---|---|---|
| "How many weeks ago did I attend the festival?" | (a) find festival fact; (b) find its date; (c) subtract; (d) express weeks | Hand 25 facts to gpt-4o-mini, hope |
| "What was my third project?" | (a) enumerate projects; (b) order chronologically; (c) pick rank 3 | Hand mixed facts, hope LLM sorts |
| "Did I tell you about my new manager before or after the promotion?" | (a) find manager fact; (b) find promotion fact; (c) compare timestamps | Hand both facts, hope ordering survives |

The reasoning steps are deterministic given the right facts. We're
asking the answerer to **both find and reason** in one shot, which is
the most error-prone configuration.

## What Hindsight does (the model)

`hindsight_api/engine/memory_engine.py:reflect_async` runs an **agentic
tool loop**:

```
agent = LLM with tools:
  - lookup(name)         → retrieve mental models by name
  - recall(query, type)  → retrieve facts (semantic + temporal)
  - learn(insight)       → create new mental model from facts
  - expand(memory_id)    → get raw chunk context for a fact

while iterations < budget * multiplier:
    response = agent.call(messages, tools_enabled=True)
    if response.tool_calls:
        execute tools, append results to messages
    else:
        break  # agent produced final answer

if no final answer:
    response = agent.call(messages, tools_enabled=False)  # force text
return response.text
```

**Key insight**: the agent decides what to retrieve and when, based on
what it already knows. A "how many weeks ago" question would naturally
trigger: `recall("festival") → recall("festival" with temporal_range
near response) → produce answer with date math`.

Compared to our single-shot approach where the LLM gets one pool of
candidates and has to extract everything.

## Astrocyte M36 — minimum viable iterative loop

Full agentic tool-loop is 1-2 weeks of work. We can ship a **simpler
2-pass loop** that captures most of the value in ~3-4 days:

### Pass 1: Initial retrieve + reason-or-defer

```
candidates = current_recall_pipeline(query)
response = llm.call(
    system=ANSWERER_SYSTEM_PROMPT,
    user=format_context(candidates) + question,
    response_format={
      "answer": str | null,
      "deferred": bool,
      "missing": str | null,  # what's missing if deferred
      "refined_query": str | null,  # better query if deferred
    }
)
if not response.deferred:
    return response.answer
```

### Pass 2: Refined re-retrieve + final answer

```
extra_candidates = current_recall_pipeline(response.refined_query)
all_candidates = dedupe(candidates + extra_candidates)
response = llm.call(
    system=ANSWERER_SYSTEM_PROMPT + "\nThis is your second pass.",
    user=format_context(all_candidates) + question,
    response_format={"answer": str}
)
return response.answer
```

### Why this captures most of Hindsight's value

- **Iterative refinement**: catches questions where first-pass retrieval missed something
- **Query refinement**: lets the LLM rewrite the query into a more retrievable form
- **Bounded cost**: 2 LLM calls + 2 retrieval calls max, no risk of unbounded agent loop
- **No new tools**: reuses existing recall pipeline, no `lookup` / `learn` / `expand` plumbing

What we DON'T get vs full Hindsight reflect:
- `learn` tool (mental model creation during reflect) — defer to M37
- `expand` tool (raw chunk fetch) — already in our context most of the time
- `lookup` tool (mental model by name) — defer to M37
- Multi-pass beyond 2 — diminishing returns per Hindsight's own data; their
  default `reflect_max_iterations` is small for non-HIGH budget

## Implementation plan

| # | Step | Effort |
|---|---|---|
| M36-1 | New `astrocyte/pipeline/reflect.py` with `reflect_answer(query, recall_fn, llm_fn) -> str` | 3-4 hr |
| M36-2 | JSON-schema response format with `deferred / missing / refined_query` | 1 hr |
| M36-3 | Bench wiring — `astrocyte_client.py` flips from single-shot to reflect | 2 hr |
| M36-4 | Tests: deferred-then-answered, no-defer-direct-answer, query-refinement-helps | 3 hr |
| M36-5 | Bench v015o | 70 min wall |

Total: **~1 day code + 1 bench cycle**.

## Acceptance criteria

- LME max_tokens_2048 cutoff ≥ 80% (target: break the v015l 74% ceiling)
- LoCoMo max_tokens_2048 ≥ 83% (no regression)
- Per-category: TR and MS each improve by ≥ +2 questions (these are the
  most-iterative LME categories)
- Latency: ≤ 2× v015n per question (we're adding one extra LLM call)

## Cost analysis

- Extra LLM call per question on the ~30% that defer
- LME bench: 90 q × 30% defer × 1 extra call × ~$0.0005 = $0.014 per bench
- LoCoMo bench: 200 q × 30% × ~$0.0005 = $0.030 per bench
- **Negligible** vs the architectural lift

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| LLM defers on every question (over-deferring) | Cap iterations at 2; first answer is forced if both pass produce no answer |
| Refined query is worse than original | Track per-bench: if refined queries underperform original, disable refinement and just append more candidates |
| Cost / latency blow-up | Strict 2-pass cap, no agentic loop |
| Defer-then-answer same as direct-answer (no actual lift) | Defer rate becomes the signal — if < 10% defer, the architecture is fine for the workload |

## What this leaves on the table (v0.16.0)

- Full agentic tool loop (Hindsight-parity)
- Mental model creation during reflect (`learn` tool — needs M37 to deliver value)
- More than 2 iterations

---

## Cycle close — executed as v015o, gate FAILED (reconstructed 2026-09-01)

This close was never written at cycle time; it is reconstructed post-hoc from
the harness code, the R2 bench trajectory, and the v0.15.0 ship records, after
the stale "Status: Design" header nearly caused a re-run of the experiment.

### What actually shipped (plan revision)

The implementation deviated from the M36-1/M36-2 design above. Instead of a
new 2-pass reason-or-defer loop in `astrocyte/pipeline/reflect.py`, M36
shipped as **per-question hybrid reflect routing** to the *already-existing*
agentic reflect loop:

- `scripts/mem0_harness/_reflect_routing.py` (NEW) — routes only temporal
  questions to reflect; everything else stays on recall+answerer. This is the
  M20 §8.3/§8.5 "+5q projected" candidate, deferred through M21.
- Routing signal is production-honest: query-analyzer `temporal_constraint`
  (regex Pass A + dateparser Pass B) OR supplementary duration/ordering
  regexes ("how many weeks", "how long ago", "before or after", …). The bench
  category label is deliberately NOT used (the module docstring records why).
- Wired into both `run_lme.py` and `run_locomo.py` via process_question
  monkey-patch, gated by `ASTROCYTE_USE_REFLECT`; hybrid is the default mode
  when the flag is on (`ASTROCYTE_USE_REFLECT_HYBRID=0` reverts to M20 all-on).

### Bench verdict — v015o

| Cycle | LME (n=90, mt_8192) | Context |
|---|---|---|
| **v015o (M36 hybrid routing)** | **62.2%** | **Lowest score of the entire v015 series** |
| v015p (next cycle, re-baseline) | 71.1% | Harness comments treat "parity with v015p" as the baseline config |
| v015w (M44, shipped) | 74.4% | Final v0.15.0 ship-floor |

Against the acceptance criteria above (LME ≥ 80%, break the ~74% ceiling):
**decisively failed** — the routing did not merely miss the gate, it regressed
~9-12pp below the surrounding cycles. Consistent with M20's structural
findings (the loop's `done` tool never fires; synthesis quality inside the
loop trails the single-shot answerer): sending temporal questions into a loop
with a broken termination architecture made them worse, not better.

### Evidence trail

- R2 trajectory `trajectory/longmemeval.json`: `v015o = 0.6222 @
  max_tokens_8192, n=90, 2026-05-23` (no notes field on the row).
- This doc §Implementation plan, M36-5: "Bench v015o" — names the cycle.
- v0.15.0 ship-decision §4 + CHANGELOG: `ASTROCYTE_USE_REFLECT` remains a
  default-OFF experimental flag in the shipped release.
- **Attribution caveat**: the trajectory row carries no config snapshot; the
  v015o = M36 mapping is inferred from this doc's plan plus cycle sequencing
  (v015n = M35 token budget → v015o next). If the archived v015o result JSON
  on R2 contradicts this, correct this section.

### Disposition

- `_reflect_routing.py` stays in-tree as an opt-in experiment surface; no
  default behavior change shipped from M36.
- **Do not re-run as-is.** Two independent attempts (M20 all-on, M36 hybrid)
  now show the current reflect loop is net-negative on this bench regardless
  of routing granularity. The prerequisite for any third attempt is fixing the
  loop's termination architecture (M20 insight #1: force a candidate answer
  per iteration, or a mandatory termination-check tool).
- The §Problem statement thesis — LME tests cross-session *reasoning*, we do
  cross-session *retrieval* — remains valid and **unsolved**: v015w's +3q came
  from M44 answerer-prompt addenda, not iterative reasoning. The reasoning gap
  is still the largest open lever on LME.
