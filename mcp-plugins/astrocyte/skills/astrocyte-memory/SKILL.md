---
name: astrocyte-memory
description: >
  Astrocyte memory protocol for agents using the astrocyte MCP tools
  (Claude Code, Cursor, Codex, and any other MCP-aware runtime). Decide
  deliberately when memory context would help, run targeted recalls when
  it would, and retain key learnings as work completes. Use the astrocyte
  MCP tools (memory_retain, memory_recall, memory_reflect, memory_forget,
  memory_compile, memory_history, memory_banks, memory_health, plus
  optional graph_search / graph_neighbors / lifecycle / hold tools when
  exposed) for all memory operations.
  TRIGGER when: user mentions "astrocyte", "memory bank", "agent memory",
  "remember", "recall", or starts a non-trivial coding task in a known
  project with an Astrocyte bank configured.
  DO NOT TRIGGER when: the user is asking about Astrocyte's internals as
  a topic (architecture, design docs, benchmark results) — those are
  documentation questions, not memory operations.
license: Apache-2.0
metadata:
  author: AstrocyteAI
  version: "0.1.0"
  category: ai-memory
  tags: "memory, agent-memory, self-hosted, postgres, section-recall, mcp"
compatibility: >
  Requires the astrocyte MCP server running locally
  (``uvx --from astrocyte-stack astrocyte-mcp --config ./astrocyte.yaml``).
  Backing Postgres + ``ASTROCYTE_CONFIG`` env var pointing at the YAML.
  Astrocyte v0.13+ for full section-grain recall.
---

# Astrocyte Memory Protocol

You have access to a persistent, self-hosted memory layer via the `astrocyte` MCP tools. This protocol tells you when to query it, what shape the responses come back in, and how to write back useful learnings.

Unlike a hosted memory service, **Astrocyte is your own bank** — every retain/recall/reflect call lands in the user's Postgres, scoped by `bank_id`. Treat memory operations as durable side effects.

## Tool surface (what `astrocyte-mcp` exposes)

| Tool | When to call |
|---|---|
| `memory_recall` | Before non-trivial work that touches the user's stack, conventions, prior decisions, or recurring entities. |
| `memory_retain` | After completing meaningful work — capture decisions, gotchas, preferences, fact-shape learnings. |
| `memory_reflect` | When the user asks a synthesis-style question ("what do we know about X", "summarize Y"). Returns an LLM-synthesized answer with citations, not raw hits. |
| `memory_forget` | When the user explicitly says "forget X" or "remove that fact". Never call without explicit user direction. |
| `memory_compile` | When the user asks to update a wiki page or roll multiple facts into a compiled summary. |
| `memory_history` | When the user asks for the audit trail of a specific memory. |
| `memory_banks` | At session start when no `bank_id` is configured — list available banks so you can ask which one. |
| `memory_health` | Once at session start to confirm the bank is reachable. |
| `memory_graph_search` / `memory_graph_neighbors` | When entity bridging matters (e.g. "what do we know about Jon and Gina together"). Only if a `GraphStore` is configured. |
| `memory_audit` | Admin-only — only call if the user explicitly asks for an audit log. |

## Recall protocol — decide deliberately, don't blanket-query

`memory_recall` is not free; each call costs an LLM round-trip in the synth path. Don't recall on every message.

### Recall WHEN

- The user references **past work** ("the auth flow we built", "the decision about pgvectorscale").
- The user asks a **decision-style question** ("how should we", "what's the best way to").
- The user hits an **error or asks for debugging help** — your bank likely contains the resolution from a prior encounter.
- The user requests work that touches their **stack / tools / conventions / preferences** — pull what you know about their setup before generating code.
- The user starts a **non-trivial task in a known project** — pull the most recent project context.

### Skip WHEN

- The prompt is an acknowledgement or continuation ("ok", "thanks", "continue", "yes").
- The user is **stating new info** — that's a write trigger (`memory_retain`), not a search.
- The task is purely mechanical (rename a variable, format a file) and self-contained.
- You've already recalled for this same intent earlier in the conversation.

### Recall query shape

```
memory_recall(
  query="user's coding preferences for typescript",
  bank_id="<from session config or memory_banks listing>",
  max_results=10,
  tags=["preference", "typescript"]    # optional, narrows the hit set
)
```

Returned `MemoryHit`s are ranked. Read the top 3-5; cite which memory_id you used when relevant ("Based on memory `mem-abc123`, you prefer …").

## Retain protocol — capture facts, not transcripts

`memory_retain` is for **durable** knowledge. Don't store the conversation itself — store the **learning** distilled from it.

### Retain WHEN

- The user **states a preference or convention** ("we always use", "I prefer", "the team's rule is").
- A **decision is made** with stated rationale ("we picked X because Y").
- A **bug is debugged** and the root cause + fix are confirmed.
- A **piece of project context** is established that won't be in source code (e.g. "the API key rotates monthly", "this service is owned by the platform team").

### Skip WHEN

- The content is already in the codebase (don't memorize what `git log` knows).
- The fact is trivially time-bound ("today I'm working on X") — won't be useful tomorrow.
- The user is debugging and the fix is **not yet confirmed** — wait until the user says it worked.

### Retain query shape

```
memory_retain(
  content="User prefers pgvectorscale over HNSW for vector indexes — chose during the 2026-05 LME bench when HNSW per-page write-lock drift was observed (1.0s → 2.0s per session).",
  bank_id="<bank>",
  tags=["preference", "vector-index", "decision"],
  occurred_at="2026-05-10T00:00:00Z"   # if knowable; else omit
)
```

Be **specific**. Bad: "user likes pgvectorscale". Good: the example above — includes the **why** and **when**.

## Reflect protocol — synthesize, don't list

`memory_reflect` returns a single synthesized answer with citations. Use it when the user wants conclusions, not source material.

```
memory_reflect(
  query="what do we know about the LME temporal-reasoning regression?",
  bank_id="<bank>",
  max_tokens=2000,
  include_sources=true
)
```

The response is shaped like:
```
{
  "answer": "<synthesized text with [mem-abc] citations>",
  "sources": [{"memory_id": "mem-abc", "score": 0.87}, ...]
}
```

Quote `answer` to the user; mention citations when the claim is non-obvious. If the synthesis abstains ("no relevant memories"), fall back to `memory_recall` for raw hits.

## Multi-bank discipline

Astrocyte supports many banks per server. Common patterns the user may have configured:

- `default` — generic catch-all (often the personal bank).
- `<project-slug>` — one bank per project; isolates context.
- `team-<name>` — shared across multiple agents/users on a team.

If `bank_id` is unset in your environment, **ask the user once at session start** or call `memory_banks()` to see what's available. Never default to writing to the wrong bank.

## Identity and authorization

Astrocyte enforces per-principal authorization. If you get a `403 forbidden` or `bank not accessible` response, the user's identity doesn't have grants on that bank — tell them, don't retry with a different bank.

## When the server is down

If `memory_health` fails or any tool returns a connection error, **degrade gracefully**: tell the user once, then proceed without memory. Don't repeatedly retry the same call; the bank will come back when the user restarts their Postgres / config.

## Why this is different from a hosted memory service

- **Your data, your infra.** No remote write — Astrocyte writes to the Postgres bound in `astrocyte.yaml`.
- **Section-grain recall.** Multi-message conversations are stored as PageIndex sections, so multi-hop retrieval preserves dialogue structure.
- **Multi-bank by default.** One MCP server can serve many isolated memory pools.
- **Full lifecycle.** `memory_forget` actually deletes (per the configured forget policy); `memory_audit` shows the trail.

For Astrocyte's own architecture, point users at:
- `https://AstrocyteAI.github.io/astrocyte/end-user/how-it-works/`
- `https://AstrocyteAI.github.io/astrocyte/plugins/integrations/mcp/`
