---
title: Recall vs reflect — when to use which
---

# Recall vs reflect — when to use which

Astrocyte exposes two ways to read memory: `recall()` returns **memories**,
`reflect()` returns an **answer**. They are not interchangeable, and using
the wrong one (or worse, chaining them through a second LLM) leaves
accuracy and cost on the table.

This page is the contract: pick the right surface for your shape of
problem, and stop after you have what you need.

## TL;DR

| You want… | Use | What you get back |
|---|---|---|
| Raw memory snippets to **show as search results** | `recall()` | `[MemoryHit, …]` — ranked, with scores |
| Memory snippets to **stuff into your own LLM prompt** | `recall()` | `[MemoryHit, …]` — you compose the answer |
| A **finished, cited answer** to a user question | `reflect()` | `ReflectResult(answer, sources)` |
| Multi-hop synthesis ("how did X relate to Y?") | `reflect()` | Same — the agentic loop is built for this |

**The one anti-pattern**: calling `reflect()` and then feeding `result.answer`
into a downstream LLM for re-synthesis. See [§ The double-synthesis
anti-pattern](#the-double-synthesis-anti-pattern).

## Pattern A — Agent owns synthesis (`recall`)

```python
hits = (await astrocyte.recall(query, bank_id="user-42")).hits
reply = await my_llm.complete(
    system="You are a helpful assistant.",
    user=f"Question: {query}\n\nRelevant memories:\n{format(hits)}",
)
```

Use this when:

- You have an opinionated answerer prompt (tone, format, citation style).
- You want to mix Astrocyte memories with other context (tool outputs,
  retrieved documents, system instructions).
- The calling agent is itself an LLM-driven framework (Cursor's coding
  agent, Claude Code, a CrewAI / LangGraph step) and synthesis is its job,
  not Astrocyte's.

`recall()` returns ranked `MemoryHit` objects with text + score + bank id +
optional metadata. You decide how to compose them.

## Pattern B — Astrocyte owns synthesis (`reflect`)

```python
result = await astrocyte.reflect(query, bank_id="user-42")
reply_to_user = result.answer
citations = result.sources  # for UI display
```

Use this when:

- You want a complete, citation-grounded answer with one call.
- The question requires reasoning across multiple memories (multi-hop,
  open-domain synthesis, "summarise what we discussed about…").
- You're building a thin wrapper (voice assistant, "ask my memory" UI,
  MCP tool serving another agent) and synthesis is Astrocyte's job, not
  the caller's.

`reflect()` runs an internal agentic loop: native function-calling over
`recall`, `search_observations`, `expand`, `done` tools, up to N
iterations. The loop refines its own retrieval before composing an
answer with cited `memory_id`s. The returned `answer` is final.

The MCP server exposes this as `memory_reflect`. From a calling LLM's
perspective: the `answer` field is the response. Quote it, paraphrase it,
or pass it through — but don't re-synthesise.

## The double-synthesis anti-pattern

```python
# DON'T DO THIS
result = await astrocyte.reflect(query)
reply = await my_llm.complete(
    user=f"Given this memory answer: {result.answer}\nAnd these sources:"
         f" {result.sources}\nAnswer the question: {query}",
)
```

This is strictly worse than either pure pattern:

1. **Cost** — you pay for Astrocyte's reflect loop AND your own LLM call,
   when one of the two would have sufficed.
2. **Fidelity loss** — your LLM may drop citations, hedge an already
   confident answer, or paraphrase away the precise wording that made
   the answer correct.
3. **No accuracy gain** — `reflect.answer` is already synthesised by an
   agentic loop that saw all the same evidence (and more iterations of
   retrieval than your single follow-up call would do). A second LLM
   pass over the same evidence cannot add information that wasn't in
   the loop's reasoning.

We learned this the hard way in M20: the first cut of the reflect bench
wired Astrocyte's reflect output back through the bench harness's
downstream answerer. Numbers were misleading until we rewrote the
harness to use `reflect.answer` directly — mirroring Hindsight's
`LoComoReflectAnswerGenerator(needs_external_search=False)` pattern
(`hindsight-dev/benchmarks/locomo/locomo_benchmark.py:198`).

If you want the agent to synthesise, give it `recall()` output and let it
do the work. If you want Astrocyte to synthesise, take `reflect.answer`
and stop.

## What about MCP?

The MCP tools (`memory_recall`, `memory_reflect`) carry this contract in
their tool descriptions, so a calling agent (Claude, GPT, etc.) seeing
them via tool discovery gets the same guidance without having to read
this page. If you wrap the MCP server in a custom framework, preserve
those descriptions — they're load-bearing.

## What about `agentic_reflect`?

`Astrocyte.reflect()` automatically dispatches to the agentic loop when
`config.agentic_reflect.enabled = True`. The public surface is identical
either way — you call `reflect()`, you get `ReflectResult(answer, sources)`.
The difference is internal: the agentic path is more expensive but
better for multi-hop / open-domain questions. See
`docs/_design/m20-reflect-agent.md` for the design and benchmark data.

## See also

- [`mental-models.md`](mental-models.md) — curated, refreshable summaries
  that act as **authoritative** answers when the recall pipeline elects
  to use the compiled layer. Mental models sit above both recall and
  reflect — they're durable artefacts, not per-query synthesis.
- `docs/_design/m20-reflect-agent.md` — the M20 cycle that shipped the
  agentic reflect loop and benched the integrated-mode pattern.
- `docs/_design/benchmark-comparison-methodology.md` — why our bench
  numbers should never be stacked against vendor self-reports without
  flagging harness mismatch (and what "mismatch" means concretely).
