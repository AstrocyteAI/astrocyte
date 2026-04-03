# Astrocytes design documentation

Astrocytes is an open-source memory framework that sits between agents and storage—it provides a stable API for retain/recall/synthesize, pluggable retrieval and memory-engine backends, and a built-in policy layer for governance and observability.

This folder (`docs/`) is the **shared design specification** for the Astrocytes framework. The same repository also contains the **parallel service implementations**: **[`astrocytes-py/`](../astrocytes-py/README.md)** (Python) and **[`astrocytes-rs/`](../astrocytes-rs/README.md)** (Rust). Those directories are the Python and Rust Astrocytes services; design documents here apply to **both** unless stated otherwise.

**Scope:** The framework is **memory + governance + provider SPIs**. It is **not** an LLM gateway and **not** an agent runtime: it does not define orchestration (graphs, tool loops, checkpoints, scheduling, multi-agent routing). Agent catalogs or “agent cards” are **not** first-class Astrocytes artifacts; map them to principals and memory banks in your integration layer. See `03-architecture-framework.md` §1 and `17-agent-framework-middleware.md`.

**Diagrams:** Architecture figures use [Mermaid](https://mermaid.js.org/) (`flowchart`, etc.). They render on GitHub, in VS Code with a Mermaid preview, and in most modern Markdown tools.

**Implementations (this repository):**

| Folder | Role |
|---|---|
| **`astrocytes-py/`** | **Python** Astrocytes service; PyPI package name **`astrocytes`**; ecosystem integrations (LangChain, MCP, …). |
| **[`astrocytes-services-py/`](../astrocytes-services-py/README.md)** | Optional **REST** ([`astrocytes-rest`](../astrocytes-services-py/astrocytes-rest/README.md)) and **[`astrocytes-pgvector`](../astrocytes-services-py/astrocytes-pgvector/README.md)**; not part of the core SPI. **Docker:** [`docker-compose.yml`](../astrocytes-services-py/docker-compose.yml); **runbook** ([`scripts/runbook-up.sh`](../astrocytes-services-py/scripts/runbook-up.sh)); **[`Makefile`](../astrocytes-services-py/Makefile)** for common Compose commands. Operations, env split (`ASTROCYTES_REST_DATABASE_URL` vs host migrate DSN), and debugging: [`astrocytes-services-py/README.md`](../astrocytes-services-py/README.md) and [doc 24 §4](./24-production-grade-http-service.md). |
| **`astrocytes-rs/`** | **Rust** Astrocytes service; native crate; same framework contract as Python (see `13-implementation-language-strategy.md`). |

Numbered files below define a **recommended reading order** and stable cross-references.

---

## Reading order (full)

| # | Document | Topic |
|---|----------|--------|
| 01 | [01-neuroscience-astrocytes.md](./01-neuroscience-astrocytes.md) | Biological metaphor and vocabulary |
| 02 | [02-design-principles.md](./02-design-principles.md) | Engineering principles mapped from neuroscience |
| 03 | [03-architecture-framework.md](./03-architecture-framework.md) | Layers, two-tier providers, SPI overview |
| 04 | [04-provider-spi.md](./04-provider-spi.md) | Retrieval, Memory Engine, LLM SPIs; outbound transport §5; multimodal §4.11 |
| 05 | [05-outbound-transport.md](./05-outbound-transport.md) | Credential gateways, HTTP/TLS proxy plugins |
| 06 | [06-identity-and-external-policy.md](./06-identity-and-external-policy.md) | IdP integration, optional external PDP / Casbin; **§8** — repo placement (grants vs PDP vs `astrocytes-rest`) |
| 07 | [07-presentation-layer-and-multimodal-services.md](./07-presentation-layer-and-multimodal-services.md) | Video/voice products **beside** the LLM SPI (Tavus-class) |
| 08 | [08-multimodal-llm-spi.md](./08-multimodal-llm-spi.md) | Vision/audio **in chat** - `ContentPart`, adapters |
| 09 | [09-policy-layer.md](./09-policy-layer.md) | Homeostasis, barriers, pruning, observability |
| 10 | [10-use-case-profiles.md](./10-use-case-profiles.md) | YAML profiles for scenarios |
| 11 | [11-built-in-pipeline.md](./11-built-in-pipeline.md) | Tier 1 intelligence pipeline |
| 12 | [12-ecosystem-and-packaging.md](./12-ecosystem-and-packaging.md) | Packages, entry points, open-core |
| 13 | [13-implementation-language-strategy.md](./13-implementation-language-strategy.md) | Parallel Python + Rust drop-in implementations |
| 14 | [14-multi-bank-orchestration.md](./14-multi-bank-orchestration.md) | Multiple memory banks |
| 15 | [15-memory-portability.md](./15-memory-portability.md) | Export / import between providers |
| 16 | [16-mcp-server.md](./16-mcp-server.md) | MCP tool integration |
| 17 | [17-agent-framework-middleware.md](./17-agent-framework-middleware.md) | LangGraph, CrewAI, etc. (memory hooks, not orchestration) |
| 18 | [18-memory-lifecycle.md](./18-memory-lifecycle.md) | TTL, compliance, legal hold, audit |
| 19 | [19-access-control.md](./19-access-control.md) | Principals, permissions, banks |
| 20 | [20-event-hooks.md](./20-event-hooks.md) | Webhooks and alerts |
| 21 | [21-memory-analytics.md](./21-memory-analytics.md) | Bank health, utilization |
| 22 | [22-evaluation.md](./22-evaluation.md) | Benchmarks and regression testing |
| 23 | [23-data-governance.md](./23-data-governance.md) | Classification, residency, DLP |
| 24 | [24-production-grade-http-service.md](./24-production-grade-http-service.md) | Production HTTP checklist; reference [`astrocytes-rest`](../astrocytes-services-py/astrocytes-rest/README.md); Compose [`docker-compose.yml`](../astrocytes-services-py/docker-compose.yml); local ops / troubleshooting **§4.5** |

---

## Shorter paths

- **New to the framework:** 01 → 02 → 03 → 04.
- **Implementing a provider:** 04, 12, 13.
- **Security and compliance:** 09, 19, 18, 23, 06.
- **Integrations (network, identity, UI):** 05, 06, 07, 08.
- **Production HTTP / BFF hosting Astrocytes:** 24, 03, 06, 19, 05, 09, 18, 23.

---

## Building this documentation site

This `docs/` folder is also the [Starlight](https://starlight.astro.build/) package. From **`docs/`**, run `pnpm install`, then `pnpm dev` to preview the site or `pnpm build` to emit **`dist/`**. The script **`scripts/sync-docs.mjs`** copies the numbered `*.md` files and this `README` into **`src/content/docs/`** for Astro (generated paths are gitignored). Deployment: [`.github/workflows/docs.yml`](../.github/workflows/docs.yml).
