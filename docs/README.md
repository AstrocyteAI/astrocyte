# Introduction

For sixty years, enterprise knowledge management has oscillated between two failing options. **Option 1:** hand-encode structure — taxonomies, ontologies, knowledge graphs built by experts. Expensive to build, impossible to maintain as reality changes. **Option 2:** skip structure entirely — wikis, file shares, and now RAG. Searchable, but unintelligent. You can ask "what do we know about the deployment pipeline?" but the system cannot tell you what it *doesn't* know, cannot resolve that "Calvin" and "the CTO" are the same person across documents, and cannot answer "what did we believe about this on March 1st?"

**Astrocyte is the third option.** LLMs extract structure automatically — entities, relationships, facts — and a deterministic harness verifies, governs, and queries that structure. Structure emerges from content; it is not hand-encoded. The system gets smarter as more is retained, without any manual curation.

Four diagnostic tests separate systems that pass the third-option bar from systems that are sophisticated RAG with extra steps:

| Test | The question | What it proves |
|------|-------------|----------------|
| **Gap analysis** | "What don't we know about X?" | Reasons about *absence* — not just retrieval of what's present |
| **Entity resolution** | "Is 'Calvin' the same person as 'the CTO'?" | Structured evidence chains, not cosine similarity |
| **Time travel** | "What did we believe about X on March 1st?" | Immutable history with as-of queries |
| **Sovereignty** | "Can this run fully on our own infrastructure?" | Self-hosted, no data leaving the org |

Astrocyte implements all four in the current `0.9.x` line. The project will declare `v1.0.0` GA after the eval gates validate those capabilities against the release bar.

## What Astrocyte does

```
Your agent ──→ Astrocyte ──→ Storage backend
  (any framework)    │       (pgvector + Apache AGE on PostgreSQL,
                     │        Qdrant, Neo4j, Elasticsearch, …)
               Policy layer
         (PII, access control, quotas,
          dedup, rate limits, audit)
```

- **retain** — ingest content, extract entities and facts, deduplicate, and store into a memory bank
- **recall** — search one or more banks and return ranked hits (semantic + keyword + graph), with optional `as_of` for point-in-time queries
- **reflect** — recall relevant memories and synthesize a natural-language answer
- **forget** — remove or archive memories, with soft-delete and `forgotten_at` timestamp for compliance
- **history** — run point-in-time recall with `as_of`
- **audit** — ask "what don't we know?" — scan a bank's coverage and return gaps, absent topics, and a coverage score
- **compile** — opt-in wiki compile that turns raw memories into durable topic pages for higher-quality recall

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
| **Time travel** | Every memory carries `retained_at` and optional `forgotten_at`; `recall(as_of=datetime)` queries the bank as it stood at any point in time |
| **Gap analysis** | `brain.audit(scope, bank_id)` reasons about absence — what topics are missing, what's thin, what contradicts — returns `AuditResult(gaps, coverage_score)` |
| **Entity resolution** | Retain-time pipeline stage: extract entities → look up graph candidates → LLM confirms with evidence quote → `EntityLink(link_type="alias_of", evidence=quote)` |
| **Apache AGE** | Preferred graph store for the PostgreSQL reference stack (`astrocyte-age`) — runs inside the same PostgreSQL instance as pgvector, zero additional operational burden |

## Current release

**v0.11.0** is the current minor release. It introduces schema-per-tenant isolation across PostgreSQL and AGE, mental models as a first-class SPI, observation scope and invalidation, intent-driven reflect routing, and the four-preset benchmark ablation matrix. The Postgres adapter has been renamed (`astrocyte-pgvector` → `astrocyte-postgres`); migration is a single `pip uninstall astrocyte-pgvector && pip install astrocyte-postgres`.

The v0.9.x line shipped the pre-GA feature surface (wiki compile, time travel, gap analysis, entity resolution, AGE graph adapter); v0.10.x added retrieval quality (HyDE, observation consolidation, multi-query gating, adversarial defense, agentic reflect, fact-level causal links, link expansion). **v1.0.0** GA will be declared after the v0.11.x eval gates in [`benchmarks/gates-baseline.json`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-py/benchmarks/gates-baseline.json) and [`gates-hindsight-informed.json`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-py/benchmarks/gates-hindsight-informed.json) pass.

Release notes: [CHANGELOG.md](https://github.com/AstrocyteAI/astrocyte/blob/main/CHANGELOG.md)

## Get started

- **[Quick Start](/end-user/quick-start/)** — install, configure, and run your first retain/recall in minutes
- **[Memory API reference](/end-user/memory-api-reference/)** — core Python and REST memory operations
- **[Configuration reference](/end-user/configuration-reference/)** — complete `astrocyte.yaml` schema
- **[Bank management](/end-user/bank-management/)** — multi-bank queries, tenant patterns, lifecycle

## Go deeper

- **[Agent framework middleware](/plugins/agent-framework-middleware/)** — integration guides for 18 frameworks (LangGraph, CrewAI, MCP, and more)
- **[Architecture](/design/architecture/)** — layered design, two-tier providers, context vs harness engineering
- **[Hindsight-informed capabilities](/design/hindsight-informed-capabilities/)** — what Astrocyte copies from Hindsight, what stays outside core, and the capability parity sequence
- **[Async task backend](/design/async-task-backend/)** — Postgres-backed worker plan for compile, consolidation, entity, audit, and export jobs
- **[Design principles](/design/design-principles/)** — engineering principles mapped from neuroscience
- **[Provider SPI](/plugins/provider-spi/)** — build your own storage or LLM adapter

## Implementations

| Package | Language | Role |
|---------|----------|------|
| [`astrocyte`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-py/README.md) | Python | Core library (PyPI) |
| [`astrocyte-gateway-py`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-services-py/astrocyte-gateway-py/README.md) | Python | Optional REST gateway |
| [`astrocyte-postgres`](https://github.com/AstrocyteAI/astrocyte/blob/main/adapters-storage-py/astrocyte-postgres/README.md) | Python | PostgreSQL + pgvector adapter (VectorStore) |
| [`astrocyte-age`](https://github.com/AstrocyteAI/astrocyte/blob/main/adapters-storage-py/astrocyte-age/README.md) | Python | Apache AGE adapter (GraphStore) — shares PostgreSQL instance with pgvector |
| [`astrocyte-rs`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-rs/README.md) | Rust | Planned native crate; currently a contract stub |

## Building this documentation site

This `docs/` folder is a [Starlight](https://starlight.astro.build/) package. From `docs/`, run `pnpm install`, then `pnpm dev` to preview or `pnpm build` to emit `dist/`. Source markdown lives in `docs/_design/`, `docs/_plugins/`, `docs/_end-user/`, and `docs/_tutorials/` — `docs/scripts/sync-docs.mjs` mirrors them into `docs/src/content/docs/` for the site. Deployment: [`.github/workflows/docs.yml`](https://github.com/AstrocyteAI/astrocyte/blob/main/.github/workflows/docs.yml).
