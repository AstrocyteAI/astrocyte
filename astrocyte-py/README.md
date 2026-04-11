![Astrocyte](https://raw.githubusercontent.com/AstrocyteAI/astrocyte/main/docs/public/logo.svg)

# Astrocyte

**Open-source memory framework for AI agents.**
Retain, recall, and synthesize — with pluggable backends, built-in governance, and 18 framework integrations.

[![PyPI](https://img.shields.io/pypi/v/astrocyte)](https://pypi.org/project/astrocyte/)
[![Python](https://img.shields.io/pypi/pyversions/astrocyte)](https://pypi.org/project/astrocyte/)
[![License](https://img.shields.io/github/license/AstrocyteAI/astrocyte)](https://github.com/AstrocyteAI/astrocyte/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-astrocyte-blue)](https://astrocyteai.github.io/astrocyte/)

---

## What is Astrocyte?

Astrocyte gives AI agents **persistent memory** — store what matters, retrieve what's relevant, synthesize answers from accumulated knowledge. It sits between your agents and their storage, providing:

- **Three operations:** `retain()`, `recall()`, `reflect()` — one API for every agent and every backend
- **Pluggable backends:** Tier 1 storage (pgvector, Pinecone, Qdrant, Neo4j) or Tier 2 engines (Mystique, Mem0, Zep, Letta)
- **Built-in governance:** PII scanning, rate limits, token budgets, circuit breakers, access control, observability
- **18 framework integrations:** LangGraph, CrewAI, OpenAI, Claude Agent SDK, Google ADK, AutoGen, and more
- **MCP server:** Any MCP-capable agent (Claude Code, Cursor, Windsurf) gets memory with zero code

## Quick start

```bash
pip install astrocyte
```

```python
from astrocyte import Astrocyte

brain = Astrocyte.from_config("astrocyte.yaml")

# Store a memory
await brain.retain("Calvin prefers dark mode", bank_id="user-123")

# Recall relevant memories
hits = await brain.recall("What are Calvin's preferences?", bank_id="user-123")

# Synthesize an answer from memory
result = await brain.reflect("Summarize what we know about Calvin", bank_id="user-123")
```

## Agent framework integrations

Astrocyte works with every major agent framework through thin middleware — one integration per framework, works with every memory backend.

| Framework | Module |
|---|---|
| LangGraph / LangChain | `astrocyte.integrations.langgraph` |
| CrewAI | `astrocyte.integrations.crewai` |
| OpenAI Agents SDK | `astrocyte.integrations.openai_agents` |
| Claude Agent SDK | `astrocyte.integrations.claude_agent_sdk` |
| Google ADK | `astrocyte.integrations.google_adk` |
| Pydantic AI | `astrocyte.integrations.pydantic_ai` |
| AutoGen / AG2 | `astrocyte.integrations.autogen` |
| Smolagents (HuggingFace) | `astrocyte.integrations.smolagents` |
| LlamaIndex | `astrocyte.integrations.llamaindex` |
| Semantic Kernel | `astrocyte.integrations.semantic_kernel` |
| DSPy | `astrocyte.integrations.dspy` |
| CAMEL-AI | `astrocyte.integrations.camel_ai` |
| BeeAI (IBM) | `astrocyte.integrations.beeai` |
| Strands Agents (AWS) | `astrocyte.integrations.strands` |
| LiveKit Agents | `astrocyte.integrations.livekit` |
| Haystack (deepset) | `astrocyte.integrations.haystack` |
| Microsoft Agent Framework | `astrocyte.integrations.microsoft_agent` |
| MCP (Claude Code, Cursor) | `astrocyte.mcp` |

## MCP server

Any MCP-capable agent gets memory with zero code integration:

```json
{
  "mcpServers": {
    "memory": {
      "command": "astrocyte-mcp",
      "args": ["--config", "astrocyte.yaml"]
    }
  }
}
```

## Built-in governance

Neuroscience-inspired policies that protect every operation — regardless of backend:

- **PII barriers** — regex, NER, or LLM-based scanning with redact/reject/warn actions
- **Rate limits & quotas** — token bucket rate limiting and daily quotas per bank
- **Circuit breakers** — automatic degraded mode when backends go down
- **Access control** — per-bank read/write/forget/admin permissions
- **Observability** — OpenTelemetry spans and Prometheus metrics on every operation
- **Data governance** — classification levels, compliance profiles (GDPR, HIPAA, PDPA)

## Multi-bank orchestration

Query across personal, team, and org banks with cascade, parallel, or first-match strategies:

```python
hits = await brain.recall(
    "What are Calvin's preferences and team policies?",
    banks=["personal", "team", "org"],
    strategy="cascade",
)
```

## Memory portability

Export and import memories between providers — no vendor lock-in:

```python
await brain.export_bank("user-123", "./backup.ama.jsonl")
await brain.import_bank("user-123", "./backup.ama.jsonl")
```

## Evaluation

Benchmark memory quality with built-in suites and DeepEval LLM-as-judge:

```python
from astrocyte.eval import MemoryEvaluator

evaluator = MemoryEvaluator(brain)
results = await evaluator.run_suite("basic", bank_id="eval-bank")
print(f"Recall precision: {results.metrics.recall_precision:.2%}")
```

## Development

From [`astrocyte-py/`](.) with the dev extra:

```bash
uv sync --extra dev
uv run ruff check astrocyte/ tests/
uv run python -m pytest tests/ -x --tb=short
```

**Git hooks (Ruff via [pre-commit](https://pre-commit.com))** — reduces CI lint failures. From the **repository root** (parent of `astrocyte-py/`):

```bash
uv sync --extra dev --directory astrocyte-py
uv run --project astrocyte-py pre-commit install
```

Hooks run automatically on `git commit`; run on all files anytime with:

```bash
uv run --project astrocyte-py pre-commit run --all-files
```

## Documentation

**[astrocyteai.github.io/astrocyte](https://astrocyteai.github.io/astrocyte/)**

- [Quick Start](https://astrocyteai.github.io/astrocyte/end-user/quick-start/)
- [Architecture](https://astrocyteai.github.io/astrocyte/design/architecture-framework/)
- [Provider SPI](https://astrocyteai.github.io/astrocyte/plugins/provider-spi/)
- [Integration guides](https://astrocyteai.github.io/astrocyte/plugins/agent-framework-middleware/) (one per framework)

## License

Apache 2.0 — see [LICENSE](https://github.com/AstrocyteAI/astrocyte/blob/main/LICENSE).
