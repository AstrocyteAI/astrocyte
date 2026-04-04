# Applied AI fellowship ↔ Astrocyte vocabulary

This note is for **educators**, **certification authors**, and **architects** who teach against vendor-neutral agentic-stack curricula (for example the [Applied AI Fellowship](https://calvinchengx.github.io/applied-ai/)) while building or reviewing **Astrocyte** deployments. It maps **course vocabulary** to **framework primitives** without requiring either side to rename concepts.

## Plane alignment


| Fellowship / stack plane             | Role                                             | Astrocyte                                                                                                                                                     |
| ------------------------------------ | ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Context & Memory**                 | Governed memory and retrieval—not “RAG only”     | Core **retain** / **recall** / **reflect**; hybrid retrieval; Tier 1 vs Tier 2 providers ([Architecture framework §2](./architecture-framework.md))            |
| **Control** (outside the agent loop) | Deterministic policy, identity surfaces, budgets | Per-bank AuthZ, policy layer, opaque **principal** from the caller; MCP tool graphs are owned by the **harness**, not Astrocyte (repository README non-goals) |
| **Runtime**                          | Orchestration, workers, sandboxes                | **Out of scope** — agent frameworks own the harness ([Architecture framework §1](./architecture-framework.md))                                                 |
| **Integrations**                     | MCP, search, APIs                                | MCP as an integration surface ([MCP server](./mcp-server.md)); memory is **invoked from** the application loop                                                 |
| **Observability**                    | Traces, evals, cost                              | Benchmarks, in-process bank health ([Bank health & utilization](./memory-analytics.md)), [Evaluation](./evaluation.md), [Event hooks](./event-hooks.md)            |
| **Data**                             | Tenancy, schemas, contracts                      | **Memory banks** and Tier 1/2 SPIs; optional **Memory export sink** for warehouse/lakehouse/history ([Memory export sink](./memory-export-sink.md), [Storage & data planes](./storage-and-data-planes.md)); not a full enterprise data platform |


## Memory framing


| Fellowship concept                                 | Meaning in teaching                                    | In Astrocyte (approximate)                                                                                                   |
| -------------------------------------------------- | ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| **Memory ≠ RAG**                                   | Cognition-oriented persistence vs one-off retrieval    | Built-in Tier 1 pipeline, policies, and **reflect**; not limited to chunk similarity                                          |
| **World / Experience / Opinion / Observation**     | Pedagogical “memory networks”                          | Model with **multiple banks**, use-case profiles, or provider semantics—there is no mandatory one-to-one schema in core       |
| **TEMPR / Hindsight** (and similar named patterns) | Named retrieval / priming patterns in course materials | Achieved through **structured retrieval**, hybrid stores, and engine providers—not a single builtin product name in this repo |
| **Harness vs context**                             | Agent loop vs what the model sees                      | Same split as **harness engineering vs context engineering** in [Architecture framework §1](./architecture-framework.md)      |


## Suggested teaching hooks

- **Context & Memory week** (architect track): Read [Architecture framework](./architecture-framework.md) §§1–3 and [Design principles](./design-principles.md); optional hands-on: Astrocyte + a Tier 1 adapter (for example PostgreSQL + pgvector) with a written **memory boundary** spec (banks, principals, recall strategies).
- **Capstone:** Keep the fellowship artifact **vendor-neutral**; cite Astrocyte only as one **example** implementation of the governed memory boundary.

## External links

- [Applied AI Fellowship](https://calvinchengx.github.io/applied-ai/) — syllabus, stack diagram, glossary.
- [Astrocyte repository](https://github.com/AstrocyteAI/astrocyte) — source and releases.

