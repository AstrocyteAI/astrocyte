# Astrocytes

**Astrocytes** is an open-source **memory framework** for AI systems. It sits between agents (or applications) and memory storage, and aims to give you a **production-shaped memory layer**: retrieval, governance, observability, and pluggable backends, with a **stable contract** you can implement twice (Python and Rust) without changing integrations.

## Goals

- **Memory as a first-class product.** Expose a clear API for retaining, recalling, and synthesizing memories, with a **built-in intelligence pipeline** when you use Tier 1 retrieval providers (embedding, multi-strategy retrieval, fusion, reranking, and related stages) so the core stays useful with “just Astrocytes + a database,” not only with a full external memory engine.
- **Pluggable infrastructure.** Support a **two-tier provider model**: Tier 1 **retrieval** adapters (vector, graph, document stores) and Tier 2 **memory engine** providers that own the full pipeline; the framework negotiates behavior and still applies **policy** and **access control**.
- **Governance and safety by design.** Enforce a **policy layer** (homeostasis, barriers, pruning, signal quality, observability) inspired by the project’s design principles, so behavior stays controlled regardless of which backend is plugged in.
- **Identity and authorization at the boundary.** Consume an **opaque principal** from your app’s authentication story; enforce **per-bank authorization** in the framework, with optional hooks to external policy engines when enterprises require them.
- **Operational reality.** Optional **outbound transport** plugins for credential gateways and enterprise HTTP/TLS/proxy setups, shared by outbound calls that need them.
- **Portable, parallel implementations.** Ship **the same framework contract** as [`astrocytes-py/`](astrocytes-py/README.md) (PyPI package **`astrocytes`**) and [`astrocytes-rs/`](astrocytes-rs/README.md) (Rust), so deployments can choose a runtime without redesigning memory semantics. Details: [`docs/13-implementation-language-strategy.md`](docs/13-implementation-language-strategy.md).

## Non-goals

Astrocytes is **not** an LLM gateway (no generic chat routing or provider normalization) and **not** an **agent runtime** (no orchestration graphs, tool loops, checkpoints, or multi-agent scheduling). Those belong in your application or in agent frameworks; Astrocytes integrates as **memory + governance + provider SPIs**. See [`docs/03-architecture-framework.md`](docs/03-architecture-framework.md) §1 and [`docs/17-agent-framework-middleware.md`](docs/17-agent-framework-middleware.md).

## Repository layout

| Path | Contents |
|------|----------|
| [`docs/`](docs/README.md) | Design specification: architecture, SPIs, policy, packaging, evaluation, governance. |
| [`astrocytes-py/`](astrocytes-py/README.md) | Python implementation of the Astrocytes service. |
| [`astrocytes-rs/`](astrocytes-rs/README.md) | Rust implementation of the Astrocytes service (same contract). |

For scope, vocabulary, and a full reading order, start with [`docs/README.md`](docs/README.md).
