# Quick Start

Get **Astrocytes** running locally: core library in Python, then an optional **Postgres + pgvector** stack with the **reference REST** API (`astrocytes-rest`).

## Prerequisites

- **Python 3.11+** (for local installs)
- **Docker** and **Docker Compose** (recommended for the reference server)
- **pnpm** (only if you are working on this documentation site under `docs/`)

## 1. Core library (Python)

From the repository root (or any environment with the repo on `PYTHONPATH`):

```bash
pip install -e astrocytes-py/
```

Use **`Astrocyte.from_config(...)`** and attach Tier 1 / Tier 2 providers in code—see the [architecture overview](/design/architecture-framework/) and [provider SPI](/plugins/provider-spi/). The library alone does **not** start an HTTP server.

### Use the same client from an AI agent

Astrocytes does **not** run the agent loop (tools, checkpoints, multi-step graphs). Your framework does. Integrations map each framework’s memory hooks to `retain` / `recall` / `reflect` through the same policy layer — see **[agent framework middleware](/plugins/agent-framework-middleware/)** for the full matrix (LangGraph, CrewAI, Pydantic AI, OpenAI Agents SDK, Claude Agent SDK, MCP, …).

Minimal **LangGraph / LangChain** shape (install `langgraph` when you build a graph):

```python
from astrocytes import Astrocyte
from astrocytes.integrations.langgraph import AstrocyteMemory

brain = Astrocyte.from_config("astrocytes.yaml")
memory = AstrocyteMemory(brain, bank_id="user-123")

await memory.save_context(
    inputs={"question": "Theme preference?"},
    outputs={"answer": "Calvin prefers dark mode."},
    thread_id="thread-abc",
)
vars_for_prompt = await memory.load_memory_variables({})
```

Per-framework guides live under **[Integrations](/plugins/integrations/langgraph/)** (start with LangGraph or MCP, depending on your harness).

## 2. Reference REST server (Docker, recommended)

The **`astrocytes-services-py/`** tree hosts Compose, **`astrocytes-rest`**, and **`astrocytes-pgvector`**.

### Fastest path

```bash
cd astrocytes-services-py
cp .env.example .env   # optional: edit ports / secrets
docker compose up --build
```

Then:

- **Liveness:** `GET http://127.0.0.1:8080/live` (port from your `.env` / compose)
- **Health (includes DB):** `GET http://127.0.0.1:8080/health`
- **OpenAPI:** `http://127.0.0.1:8080/docs`

From the **repo root**:

```bash
docker compose -f astrocytes-services-py/docker-compose.yml --env-file astrocytes-services-py/.env up --build
```

### Production-shaped runbook (migrations + HNSW)

For SQL-owned schema and ANN indexes, use the runbook script and overlays documented in the services README:

```bash
./astrocytes-services-py/scripts/runbook-up.sh
```

Details, troubleshooting, and environment split (**`ASTROCYTES_REST_DATABASE_URL`** vs migrate DSN) are in **[`astrocytes-services-py/README.md`](https://github.com/AstrocyteAI/astrocytes/blob/main/astrocytes-services-py/README.md)** on GitHub.

## 3. Next steps

- **Agent + memory patterns/tutorials:** [100 Agents in 100 Days](/tutorials/100-agents-in-100-days/).
- **Operate and harden HTTP:** [Production-grade reference server](/end-user/production-grade-http-service/) (security, auth, grants, observability).
- **Implement a provider or transport:** [Plugin developer docs](/plugins/provider-spi/) (SPI, entry points, packaging).
- **Understand the design:** start with [Neuroscience & vocabulary](/design/neuroscience-astrocytes/) and [Architecture](/design/architecture-framework/).
