# Astrocytes

**Astrocytes** is an open-source **memory framework** for AI systems. It sits between agents (or applications) and memory storage, and aims to give you a **production-shaped memory layer**: retrieval, governance, observability, and pluggable backends, with a **stable contract** you can implement twice (Python and Rust) without changing integrations.

## Neuroscience inspiration

In the brain, **neurons** are the fast signaling substrate for perception, action, and learning. **Astrocytes** are glial cells that were once treated as passive support; contemporary work casts them as **active partners** in circuit function: they help regulate the **extracellular milieu** (for example ion and neurotransmitter clearance), participate in the **tripartite synapse** alongside pre- and postsynaptic elements, link activity to **metabolism and blood flow**, and take part in **pruning**, repair, and barrier-like interfaces. They operate on **slower, integrative timescales** than spike-driven signaling, shaping the conditions under which neurons operate.

This project uses that picture as **engineering metaphor**, not as a literal simulation of cells. We pair **fast cognitive channels** (your agents and models) with a **mediating layer** focused on **memory, boundaries, and homeostasis** - clearing and consolidating context, enforcing limits, observing load, and maintaining stable interfaces between “neural” computation and the wider environment. The goal is **tripartite stewardship** at the agent-memory exchange: the same role the docs call the framework’s “third party” at the synapse. For the biology summary see [`docs/_design/neuroscience-astrocytes.md`](docs/_design/neuroscience-astrocytes.md); for how those ideas map to design rules see [`docs/_design/design-principles.md`](docs/_design/design-principles.md).

## Goals

- **Memory as a first-class product.** Expose a clear API for retaining, recalling, and synthesizing memories, with a **built-in intelligence pipeline** when you use Tier 1 retrieval providers (embedding, multi-strategy retrieval, fusion, reranking, and related stages) so the core stays useful with “just Astrocytes + a database,” not only with a full external memory engine.
- **Pluggable infrastructure.** Support a **two-tier provider model**: Tier 1 **retrieval** adapters (vector, graph, document stores) and Tier 2 **memory engine** providers that own the full pipeline; the framework negotiates behavior and still applies **policy** and **access control**.
- **Governance and safety by design.** Enforce a **policy layer** (homeostasis, barriers, pruning, signal quality, observability) inspired by the project’s design principles, so behavior stays controlled regardless of which backend is plugged in.
- **Identity and authorization at the boundary.** Consume an **opaque principal** from your app’s authentication story; enforce **per-bank authorization** in the framework, with optional hooks to external policy engines when enterprises require them.
- **Operational reality.** Optional **outbound transport** plugins for credential gateways and enterprise HTTP/TLS/proxy setups, shared by outbound calls that need them.
- **Portable, parallel implementations.** Ship **the same framework contract** as [`astrocytes-py/`](astrocytes-py/README.md) (PyPI package **`astrocytes`**) and [`astrocytes-rs/`](astrocytes-rs/README.md) (Rust), so deployments can choose a runtime without redesigning memory semantics. Details: [`docs/_design/implementation-language-strategy.md`](docs/_design/implementation-language-strategy.md).

## Non-goals

Astrocytes is **not** an LLM gateway (no generic chat routing or provider normalization) and **not** an **agent runtime** (no orchestration graphs, tool loops, checkpoints, or multi-agent scheduling). Those belong in your application or in agent frameworks; Astrocytes integrates as **memory + governance + provider SPIs**. For **agent cards** and similar catalog metadata, the design centers on a **simple mapping** to **principals and memory banks** (config + integration helpers), not on hosting the catalog—see [`docs/_design/architecture-framework.md`](docs/_design/architecture-framework.md) §1 and [`docs/_plugins/agent-framework-middleware.md`](docs/_plugins/agent-framework-middleware.md).

## Repository layout

| Path | Contents |
|------|----------|
| [`docs/`](docs/README.md) | Design specification: architecture, SPIs, policy, packaging, evaluation, governance. |
| [`astrocytes-py/`](astrocytes-py/README.md) | Python implementation of the Astrocytes service. |
| [`astrocytes-services-py/`](astrocytes-services-py/README.md) | Optional Python **services**: reference REST server (**[`astrocytes-rest/`](astrocytes-services-py/astrocytes-rest/README.md)**) and **PostgreSQL + pgvector** adapter (**[`astrocytes-pgvector/`](astrocytes-services-py/astrocytes-pgvector/README.md)**). Compose for API + DB: [`astrocytes-services-py/docker-compose.yml`](astrocytes-services-py/docker-compose.yml). |
| [`astrocytes-rs/`](astrocytes-rs/README.md) | Rust implementation of the Astrocytes service (same contract). |

For scope, vocabulary, and a full reading order, start with [`docs/README.md`](docs/README.md).
