# Astrocytes design documentation

Astrocytes is an open-source memory framework that sits between agents and storage—it provides a stable API for retain/recall/synthesize, pluggable retrieval and memory-engine backends, and a built-in policy layer for governance and observability.

This folder (`docs/`) is the **shared design specification** for the Astrocytes framework. The same repository also contains the **parallel service implementations**: **[`astrocytes-py/`](../astrocytes-py/README.md)** (Python) and **[`astrocytes-rs/`](../astrocytes-rs/README.md)** (Rust). Those directories are the Python and Rust Astrocytes services; design documents here apply to **both** unless stated otherwise.

**Scope:** The framework is **memory + governance + provider SPIs**. It is **not** an LLM gateway and **not** an agent runtime: it does not define orchestration (graphs, tool loops, checkpoints, scheduling, multi-agent routing). How that split maps to **context engineering** (what the model sees) vs **harness engineering** (how the agent loop runs) — and where Astrocytes sits between them — is spelled out in [Architecture framework](./_design/architecture-framework.md) §1 (*Context engineering vs harness engineering*). **Agent cards** and **agent catalogs** are a common way to describe deployable agents; Astrocytes does not host the catalog or run the agent graph, but it **is** designed so card identity feeds memory through an explicit, repeatable **mapping to principal + memory bank** (declarative config and thin resolver helpers—see `architecture-framework.md` §1 and `agent-framework-middleware.md`). Vendor-specific card fields that are irrelevant to memory stay outside the core contract.

**Diagrams:** Architecture figures use [Mermaid](https://mermaid.js.org/) (`flowchart`, etc.). They render on GitHub, in VS Code with a Mermaid preview, and in most modern Markdown tools.

**Implementations (this repository):**

| Folder | Role |
|---|---|
| **`astrocytes-py/`** | **Python** Astrocytes service; PyPI package name **`astrocytes`**; ecosystem integrations (LangChain, MCP, …). |
| **[`astrocytes-services-py/`](../astrocytes-services-py/README.md)** | Optional **REST** ([`astrocytes-rest`](../astrocytes-services-py/astrocytes-rest/README.md)) and **[`astrocytes-pgvector`](../astrocytes-services-py/astrocytes-pgvector/README.md)**; not part of the core SPI. **Docker:** [`docker-compose.yml`](../astrocytes-services-py/docker-compose.yml); **runbook** ([`scripts/runbook-up.sh`](../astrocytes-services-py/scripts/runbook-up.sh)); **[`Makefile`](../astrocytes-services-py/Makefile)** for common Compose commands. Operations, env split (`ASTROCYTES_REST_DATABASE_URL` vs host migrate DSN), and debugging: [`astrocytes-services-py/README.md`](../astrocytes-services-py/README.md) and [Production-grade reference server §4](./_end-user/production-grade-http-service.md). |
| **`astrocytes-rs/`** | **Rust** Astrocytes service; native crate; same framework contract as Python (see `implementation-language-strategy.md`). |

**Source layout:** Spec markdown lives in **`docs/_design/`**, **`docs/_plugins/`**, and **`docs/_end-user/`** (underscore-prefixed authoring folders). Tutorials stay under **`docs/_tutorials/`**. **`scripts/sync-docs.mjs`** mirrors them into `src/content/docs/{design,plugins,end-user,tutorials}/` (gitignored) so published URLs stay **`/design/…`**, **`/plugins/…`**, etc. Cross-references in prose often use backticked filenames like `` `architecture-framework.md` `` as stable identifiers.

**Authored hubs (non-numbered):** [Quick Start](./_end-user/quick-start.md), [100 Agents in 100 Days](./_tutorials/100-agents-in-100-days.md).

---

## 1. End User Documentation

| # | Title | Topic |
|---|-------|--------|
| 1 | [Quick Start](./_end-user/quick-start.md) | Install core library; Docker Compose + reference REST |
| 2 | [Production-grade HTTP service](./_end-user/production-grade-http-service.md) | Production HTTP checklist; `astrocytes-rest`; Compose; ops **§4.5** |

---

## 2. Plugin Developer Documentation

| # | Title | Topic |
|---|-------|--------|
| 1 | [Provider SPI: Retrieval, Memory Engine, LLM, and Outbound Transport interfaces](./_plugins/provider-spi.md) | Retrieval, Memory Engine, LLM SPIs; outbound transport §5; multimodal §4.11 |
| 2 | [Outbound transport plugins (credential gateways)](./_plugins/outbound-transport.md) | Credential gateways, HTTP/TLS proxy plugins |
| 3 | [Multimodal LLM SPI and DTOs](./_plugins/multimodal-llm-spi.md) | Vision/audio **in chat** — `ContentPart`, adapters |
| 4 | [Ecosystem, packaging, and open-core model](./_plugins/ecosystem-and-packaging.md) | Packages, entry points, open-core |
| 5 | [Agent framework middleware](./_plugins/agent-framework-middleware.md) | LangGraph, CrewAI, etc. (memory hooks, not orchestration) |

---

## 3. Design Documentation

| # | Title | Topic |
|---|-------|--------|
| 1 | [Astrocytes in neuroscience](./_design/neuroscience-astrocytes.md) | Biological metaphor and vocabulary |
| 2 | [Design principles for “AI astrocytes” inspired by neuroscience](./_design/design-principles.md) | Engineering principles mapped from neuroscience |
| 3 | [Astrocytes framework architecture](./_design/architecture-framework.md) | Layers, two-tier providers, SPI overview |
| 4 | [Identity integration and external access policy](./_design/identity-and-external-policy.md) | IdP integration, optional external PDP / Casbin; **§8** — repo placement |
| 5 | [Presentation layer: multimodal, voice, and video services](./_design/presentation-layer-and-multimodal-services.md) | Video/voice products **beside** the LLM SPI (Tavus-class) |
| 6 | [Policy layer: the load-bearing astrocyte](./_design/policy-layer.md) | Homeostasis, barriers, pruning, observability |
| 7 | [Use-case profiles: specialized astrocyte subtypes](./_design/use-case-profiles.md) | YAML profiles for scenarios |
| 8 | [Built-in intelligence pipeline](./_design/built-in-pipeline.md) | Tier 1 intelligence pipeline |
| 9 | [Implementation language strategy](./_design/implementation-language-strategy.md) | Parallel Python + Rust drop-in implementations |
| 10 | [Multi-bank orchestration](./_design/multi-bank-orchestration.md) | Multiple memory banks |
| 11 | [Memory portability](./_design/memory-portability.md) | Export / import between providers |
| 12 | [MCP server integration](./_design/mcp-server.md) | MCP tool integration |
| 13 | [Memory lifecycle management](./_design/memory-lifecycle.md) | TTL, compliance, legal hold, audit |
| 14 | [Access control](./_design/access-control.md) | Principals, permissions, banks |
| 15 | [Sandbox awareness and memory-API exfiltration](./_design/sandbox-awareness-and-exfiltration.md) | Sandbox context, egress, BFF; related reading on isolation |
| 16 | [Event hooks](./_design/event-hooks.md) | Webhooks and alerts |
| 17 | [Memory analytics](./_design/memory-analytics.md) | Bank health, utilization |
| 18 | [Evaluation and benchmarking](./_design/evaluation.md) | Benchmarks and regression testing |
| 19 | [Data governance and privacy](./_design/data-governance.md) | Classification, residency, DLP |

---

## 4. Tutorials & Examples

| # | Title | Topic |
|---|-------|--------|
| 1 | [100 Agents in 100 Days](./_tutorials/100-agents-in-100-days.md) | Series hub: *100 Agents in 100 Days* (daily tutorials, planned) |

Add new days as Markdown files under [`_tutorials/`](./_tutorials/) and link them from the hub (or use a `day-NN-*.md` convention).

---

## Shorter paths

Numbers below refer to the **#** column within each section’s table above (§1 End user, §2 Plugins, §3 Design).

- **New to the framework:** Design **1 → 2 → 3**, then Plugins **1** or End user **2**.
- **Implementing a provider:** Plugins **1**, **4**; Design **9** (*implementation language strategy*).
- **Security and compliance:** Design **6**, **14**, **13**, **19**, **15**, **4** (*policy, access control, memory lifecycle, data governance, sandbox/exfiltration, identity*).
- **Integrations (network, identity, UI):** Plugins **2**, Design **4**, **5**, Plugins **3**.
- **Production HTTP / BFF hosting Astrocytes:** End user **2**; Design **3**, **4**, **14**, **6**, **13**, **19**, **15**; Plugins **2**.

---

## Building this documentation site

This `docs/` folder is also the [Starlight](https://starlight.astro.build/) package. From **`docs/`**, run `pnpm install`, then `pnpm dev` to preview the site or `pnpm build` to emit **`dist/`**. The script **`scripts/sync-docs.mjs`** reads specs from **`docs/_design/`**, **`docs/_plugins/`**, **`docs/_end-user/`**, and **`docs/_tutorials/`** and mirrors them into **`src/content/docs/{design,plugins,end-user,tutorials}/`**, and writes **`introduction.md`** from this `README` (generated paths are gitignored). Deployment: [`.github/workflows/docs.yml`](../.github/workflows/docs.yml).
