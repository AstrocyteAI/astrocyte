# M36 — Reflect / iterative reasoning loop

**Status**: Design (2026-05-22)
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
| Defer-then-answer same as direct-answer (no actual lift) | Defer rate becomes the signal — if <10% defer, the architecture is fine for the workload |

## What this leaves on the table (v0.16.0)

- Full agentic tool loop (Hindsight-parity)
- Mental model creation during reflect (`learn` tool — needs M37 to deliver value)
- More than 2 iterations
