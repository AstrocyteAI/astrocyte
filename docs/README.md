# Introduction

Astrocyte is an open-source memory framework that sits between AI agents and storage. It provides a stable API for **retain**, **recall**, and **reflect** (synthesize), pluggable retrieval and memory-engine backends, and a built-in policy layer for governance and observability.

## What Astrocyte does

```
Your agent ──→ Astrocyte ──→ Storage backend
  (any framework)    │            (pgvector, Qdrant, Neo4j, …)
                     │
               Policy layer
         (PII, access control, quotas,
          dedup, rate limits, audit)
```

- **retain** — ingest content, extract facts, deduplicate, and store into a memory bank
- **recall** — search one or more banks and return ranked hits (semantic + keyword + graph)
- **reflect** — recall relevant memories and synthesize a natural-language answer
- **forget** — remove or archive memories (with compliance audit support)

Astrocyte is **not** an LLM gateway and **not** an agent runtime. It does not define orchestration (graphs, tool loops, checkpoints). Your agent framework owns the loop; Astrocyte owns long-term memory, governed retrieval, and synthesis.

## Three deployment models

| Model | Best for | How |
|-------|----------|-----|
| **Library** | Prototyping, single-process agents, notebooks | `pip install astrocyte` — memory runs in-process |
| **Standalone gateway** | Multi-agent deployments, microservices, shared memory | Docker Compose — REST API at `/v1/retain`, `/v1/recall`, `/v1/reflect` |
| **Gateway plugin** | Orgs routing LLM traffic through Kong, APISIX, or Azure APIM | Thin Lua/XML shim intercepts `/chat/completions` — zero agent code changes |

## Key concepts

| Concept | What it means |
|---------|---------------|
| **Bank** | Isolated namespace for memories — provides tenant isolation, access control boundaries, and per-bank configuration |
| **Policy layer** | PII scanning, rate limits, token budgets, deduplication, access control — applied to every operation |
| **Provider SPI** | Pluggable backends for storage (VectorStore, GraphStore, DocumentStore) and LLM (complete, embed) |
| **MIP** | Memory Intent Protocol — declarative routing rules that determine which bank, tags, and policies apply |

## Current release

**v0.8.x** (latest tag: **`v0.8.0`**): Tier 1 storage adapters (pgvector, Qdrant, Neo4j, Elasticsearch), standalone HTTP gateway, recall authority, ingest connectors (Kafka, Redis streams, GitHub poll), and gateway plugins (Kong, APISIX, Azure APIM).

Release notes: [CHANGELOG.md](https://github.com/AstrocyteAI/astrocyte/blob/main/CHANGELOG.md)

## Get started

- **[Quick Start](/end-user/quick-start/)** — install, configure, and run your first retain/recall in minutes
- **[Memory API reference](/end-user/memory-api-reference/)** — full signatures for retain, recall, reflect, and forget
- **[Configuration reference](/end-user/configuration-reference/)** — complete `astrocyte.yaml` schema
- **[Bank management](/end-user/bank-management/)** — multi-bank queries, tenant patterns, lifecycle

## Go deeper

- **[Agent framework middleware](/plugins/agent-framework-middleware/)** — integration guides for 18 frameworks (LangGraph, CrewAI, MCP, and more)
- **[Architecture](/design/architecture/)** — layered design, two-tier providers, context vs harness engineering
- **[Design principles](/design/design-principles/)** — engineering principles mapped from neuroscience
- **[Provider SPI](/plugins/provider-spi/)** — build your own storage or LLM adapter

## Implementations

| Package | Language | Role |
|---------|----------|------|
| [`astrocyte`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-py/README.md) | Python | Core library (PyPI) |
| [`astrocyte-gateway-py`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-services-py/astrocyte-gateway-py/README.md) | Python | Optional REST gateway |
| [`astrocyte-pgvector`](https://github.com/AstrocyteAI/astrocyte/blob/main/adapters-storage-py/astrocyte-pgvector/README.md) | Python | PostgreSQL + pgvector adapter |
| [`astrocyte-rs`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-rs/README.md) | Rust | Native crate (same contract as Python) |

## Building this documentation site

This `docs/` folder is a [Starlight](https://starlight.astro.build/) package. From `docs/`, run `pnpm install`, then `pnpm dev` to preview or `pnpm build` to emit `dist/`. Source markdown lives in `docs/_design/`, `docs/_plugins/`, `docs/_end-user/`, and `docs/_tutorials/` — the script `scripts/sync-docs.mjs` mirrors them into `src/content/docs/` for the site. Deployment: [`.github/workflows/docs.yml`](https://github.com/AstrocyteAI/astrocyte/blob/main/.github/workflows/docs.yml).
