# Frequently asked questions

Quick answers to common questions. Each answer links to the full guide for details.

---

## Before adopting

### Where are my memories stored?

In whatever storage backend you configure. Out of the box, Astrocyte uses an **in-memory store** (a Python dict) — fast for prototyping but not persistent. For production, configure a Tier 1 adapter like [pgvector](/end-user/storage-backend-setup/#pgvector), [Qdrant](/end-user/storage-backend-setup/#qdrant), [Neo4j](/end-user/storage-backend-setup/#neo4j), or [Elasticsearch](/end-user/storage-backend-setup/#elasticsearch). Memories are rows/vectors in your database, not in Astrocyte itself.

### Do I need a database to get started?

No. The default in-memory store works with zero setup — just `pip install astrocyte` and start calling `retain()` / `recall()`. You only need a database when you want persistence across restarts. See [Quick Start](/end-user/quick-start/).

### What is a bank?

A bank is an isolated namespace for memories — like a folder. Each bank has its own access control, rate limits, and PII policies. Common patterns: one bank per user (tenant isolation), one per team (shared knowledge), or one per agent (task isolation). Banks are created automatically on first use. See [Bank management](/end-user/bank-management/).

### Is Astrocyte an LLM gateway? An agent runtime?

Neither. Astrocyte is a **memory framework** — it sits between your agent and your storage backend. It does not route LLM traffic (that's LiteLLM, OpenRouter, etc.) and does not run agent loops (that's LangGraph, CrewAI, etc.). Astrocyte owns long-term memory, governed retrieval, and synthesis. See [How it works](/end-user/how-it-works/).

### What agent frameworks are supported?

18 frameworks have integration guides: LangGraph, CrewAI, Pydantic AI, OpenAI Agents SDK, Claude Agent SDK, Google ADK, AutoGen/AG2, Smolagents, LlamaIndex, Strands, Semantic Kernel, DSPy, CAMEL-AI, BeeAI, Microsoft Agent, LiveKit, Haystack, and MCP. All integrations ship inside the `astrocyte` package — no separate installs. See [Agent framework middleware](/plugins/agent-framework-middleware/).

### Is it open source? What's the license?

Yes. The core library (`astrocyte`), all Tier 1 storage adapters, the gateway, and gateway plugins are **Apache 2.0**. Optional premium features (Tier 2 memory engines like Mystique) are commercial. See [Ecosystem & packaging](/plugins/ecosystem-and-packaging/).

### How is Astrocyte different from just using RAG?

RAG is one retrieval pattern — embed chunks, search by similarity, stuff into a prompt. Astrocyte does that and more: fact extraction, deduplication, multi-store hybrid retrieval (vector + keyword + graph), reranking, synthesis (reflect), multi-bank orchestration, PII scanning, access control, and lifecycle management. Think of it as **governed memory**, not just retrieval. See [How it works](/end-user/how-it-works/).

---

## During setup

### What's the difference between library, gateway, and gateway plugin?

| Model | What it is | When to use |
|-------|-----------|-------------|
| **Library** | `pip install astrocyte` — memory runs in your Python process | Prototyping, single-process agents, notebooks |
| **Standalone gateway** | Docker Compose REST API — agents call `/v1/retain`, `/v1/recall` over HTTP | Multi-agent, microservices, shared memory |
| **Gateway plugin** | Lua/XML shim for Kong, APISIX, or Azure APIM — intercepts `/chat/completions` | Orgs already routing LLM traffic through an API gateway |

All three use the same core and policy layer. See [Quick Start](/end-user/quick-start/) for setup instructions.

### Which storage backend should I choose?

| Backend | Best for |
|---------|----------|
| **In-memory** (default) | Prototyping, tests, demos |
| **pgvector** | Production — if you already run PostgreSQL |
| **Qdrant** | Production — if you want a dedicated vector database |
| **Neo4j** | When you need relationship/graph queries alongside vector search |
| **Elasticsearch** | When you need strong full-text/keyword search |

Most teams start with pgvector. See [Storage backend setup](/end-user/storage-backend-setup/).

### Do I need an LLM provider configured?

For **retain**, yes — the pipeline uses an LLM to generate embeddings (and optionally for fact extraction). For **recall**, embeddings are needed for query vectors. For **reflect**, an LLM generates the synthesized answer.

The default config uses a **mock LLM** (returns empty embeddings). For real use, configure the built-in OpenAI provider or install `astrocyte-llm-litellm` for Anthropic, Bedrock, Vertex, Ollama, and other LiteLLM-supported endpoints. See [Configuration reference](/end-user/configuration-reference/).

### What happens if I don't configure anything — what are the defaults?

- **Storage:** in-memory (not persistent)
- **LLM:** mock (no real embeddings or synthesis)
- **Auth:** dev mode (trusts client-supplied principal header)
- **PII scanning:** off
- **Rate limits:** none

This is enough to explore the API. For anything beyond local prototyping, configure at least a storage backend and LLM provider. See [Configuration reference](/end-user/configuration-reference/).

### Can I use Astrocyte without Docker?

Yes. The library mode is just a Python package — `pip install astrocyte`. Docker is only needed for the standalone gateway deployment model. See [Quick Start](/end-user/quick-start/).

---

## Using it

### What's the difference between recall and reflect?

**recall** returns raw memory hits — ranked list of text chunks with scores, metadata, and bank IDs. Your application decides how to use them.

**reflect** calls recall internally, then passes the hits to an LLM to synthesize a natural-language answer. Use reflect when you want an interpreted response rather than raw search results.

See [Memory API reference](/end-user/memory-api-reference/).

### How does deduplication work?

During retain, Astrocyte checks if the incoming content is semantically similar to existing memories in the same bank. If a chunk matches above the dedup threshold, it is skipped (or merged) instead of stored as a duplicate. This prevents the same fact from accumulating across repeated conversations. The threshold is configurable via `signal_quality` in [Configuration reference](/end-user/configuration-reference/).

### How does PII detection work? Can I disable it?

PII detection runs as a **barrier** in the policy layer. It supports regex patterns (credit cards, SSNs, emails, etc.) and optional LLM-based detection for context-dependent PII. Actions per PII type: `redact`, `warn`, `reject`, or `allow`.

To disable: omit the `barriers.pii` section from `astrocyte.yaml`, or set `pii_scan: false` on a per-bank basis. See [Configuration reference](/end-user/configuration-reference/).

### What is MIP and do I need it?

**MIP (Memory Intent Protocol)** is a declarative routing layer. You write YAML rules that automatically route memories to the right bank with the right tags and policies — without routing logic in your application code.

You don't need MIP if you're passing `bank_id` explicitly in every call. MIP becomes valuable when you have multiple banks, complex routing logic, or compliance rules that should be enforced consistently. See [MIP developer guide](/plugins/mip-developer-guide/).

### Can I query across multiple banks at once?

Yes. Pass `banks=["bank-a", "bank-b", "bank-c"]` to `recall()` or `reflect()` with a strategy:

- **parallel** — search all banks at once, fuse by score
- **cascade** — search in order, stop when enough results
- **first_match** — return from the first bank with hits

See [Bank management](/end-user/bank-management/#multi-bank-queries).

### How do I delete a user's data (GDPR)?

Use `forget()` with `compliance=True`:

```python
await brain.forget(
    "user-alice",
    scope="all",
    compliance=True,
    reason="GDPR erasure request #4821",
)
```

This deletes all memories in the bank and logs the operation for audit. You can also delete by specific IDs, tags, or date range. See [Memory API reference](/end-user/memory-api-reference/#forget----remove-memories).

---

## Operating and scaling

### Is memory persistent across restarts?

Only if you configure a persistent storage backend (pgvector, Qdrant, Neo4j, Elasticsearch). The default in-memory store loses all data on restart. See [Storage backend setup](/end-user/storage-backend-setup/).

### How do I migrate from in-memory to PostgreSQL?

1. Install the adapter: `pip install astrocyte-postgres`
2. Update `astrocyte.yaml`:

```yaml
vector_store: postgres
vector_store_config:
  dsn: postgresql://localhost:5432/astrocyte
```

3. Restart. New memories go to PostgreSQL. Existing in-memory data is not migrated (it was ephemeral). For production migrations between persistent backends, see [Memory portability](/design/memory-portability/).

### Can multiple agents share the same memory?

Yes. The standalone gateway is designed for this — multiple agents connect over HTTP and share memory banks. Access control determines which agent can read/write which banks. With MCP, multiple agents can connect to the same `astrocyte-mcp` server via SSE transport. See [MCP integration](/plugins/integrations/mcp/).

### What metrics and health endpoints are available?

The standalone gateway exposes:

| Endpoint | Purpose |
|----------|---------|
| `GET /live` | Liveness probe |
| `GET /health` | Readiness probe (checks DB) |
| `GET /health/ingest` | Ingest source health |

Prometheus metrics are exposed per bank: memory count, storage bytes, retain/recall totals, recall latency. See [Monitoring & observability](/end-user/monitoring-and-observability/).

### How do I back up and restore memories?

Two approaches:

- **Database-level:** Back up your PostgreSQL/Qdrant/etc. database using its native tools. This is the simplest and most reliable method.
- **AMA export/import:** Astrocyte's memory portability format for migrating between providers. See [Memory portability](/design/memory-portability/).

For ongoing export to a data warehouse or lakehouse, see [Memory export sink](/design/memory-export-sink/).
