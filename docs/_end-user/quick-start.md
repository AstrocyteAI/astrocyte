# Quick Start

Get **Astrocyte** running locally: core library in Python, then an optional **Postgres + pgvector** stack with the **reference REST** API (`astrocyte-rest`).

## Prerequisites

- **Python 3.11+** (for local installs)
- **Docker** and **Docker Compose** (recommended for the reference server)
- **pnpm** (only if you are working on this documentation site under `docs/`)

## 1. Core library (Python)

From the repository root (or any environment with the repo on `PYTHONPATH`):

```bash
pip install -e astrocyte-py/
```

Use **`Astrocyte.from_config(...)`** and attach Tier 1 / Tier 2 providers in code—see the [architecture overview](/design/architecture-framework/) and [provider SPI](/plugins/provider-spi/). The library alone does **not** start an HTTP server.

### Use the same client from an AI agent

Astrocyte does **not** run the agent loop (tools, checkpoints, multi-step graphs). Your framework does. Integrations map each framework’s memory hooks to `retain` / `recall` / `reflect` through the same policy layer — see **[agent framework middleware](/plugins/agent-framework-middleware/)** for the full matrix (LangGraph, CrewAI, Pydantic AI, OpenAI Agents SDK, Claude Agent SDK, MCP, …).

Minimal **LangGraph / LangChain** shape (install `langgraph` when you build a graph):

```python
from astrocyte import Astrocyte
from astrocyte.integrations.langgraph import AstrocyteMemory

brain = Astrocyte.from_config("astrocyte.yaml")
memory = AstrocyteMemory(brain, bank_id="user-123")

await memory.save_context(
    inputs={"question": "Theme preference?"},
    outputs={"answer": "Calvin prefers dark mode."},
    thread_id="thread-abc",
)
vars_for_prompt = await memory.load_memory_variables({})
```

Per-framework guides live under **[Integrations](/plugins/integrations/langgraph/)** (start with LangGraph or MCP, depending on your harness).

## 2. Declarative routing with MIP

The **Memory Intent Protocol (MIP)** lets you declare routing rules so Astrocyte decides which bank, tags, and policies to apply — without routing logic in your application code.

Create a `mip.yaml` alongside your `astrocyte.yaml`:

```yaml
# mip.yaml
version: "1.0"

banks:
  - id: "student-{student_id}"
    description: Per-student academic memory
    compliance: pdpa

rules:
  # Compliance — PII always goes to encrypted bank, cannot be overridden
  - name: pii-lockdown
    priority: 1
    override: true
    match:
      pii_detected: true
    action:
      bank: private-encrypted
      tags: [pii, compliance]
      retain_policy: redact_before_store

  # Student answers route to per-student banks automatically
  - name: student-answer
    priority: 10
    match:
      all:
        - content_type: student_answer
        - metadata.student_id: present
    action:
      bank: "student-{metadata.student_id}"
      tags: ["{metadata.topic}"]
```

Reference it from `astrocyte.yaml` — also configure PII detection for your region:

```yaml
# astrocyte.yaml
profile: personal
compliance_profile: pdpa          # Pre-built GDPR, HIPAA, or PDPA defaults

barriers:
  pii:
    mode: rules_then_llm          # Regex first, LLM fallback for context-dependent PII
    countries: [SG, IN, AU]       # Enable NRIC, Aadhaar, TFN patterns
    type_overrides:
      credit_card: { action: reject }   # Block credit cards outright
      name: { action: warn }            # Log names but don't redact

mip_config_path: ./mip.yaml
```

Now routing happens automatically:

```python
brain = Astrocyte.from_config("astrocyte.yaml")

# MIP routes this to bank "student-stu-42" with tag "algebra"
await brain.retain(
    "2x + 3 = 7, so x = 2",
    bank_id="default",  # MIP overrides this
    metadata={"student_id": "stu-42", "topic": "algebra"},
    content_type="student_answer",
)
```

Mechanical rules resolve deterministically with zero LLM cost. When no rule matches, the intent layer can optionally ask an LLM to decide. See the full [MIP design doc](/design/memory-intent-protocol/) for the match DSL, override hierarchy, and intent policy.

## 3. Reference REST server (Docker, recommended)

The **`astrocyte-services-py/`** tree hosts Compose, **`astrocyte-rest`**, and **`astrocyte-pgvector`**.

### Fastest path

```bash
cd astrocyte-services-py
cp .env.example .env   # optional: edit ports / secrets
docker compose up --build
```

Then:

- **Liveness:** `GET http://127.0.0.1:8080/live` (port from your `.env` / compose)
- **Health (includes DB):** `GET http://127.0.0.1:8080/health`
- **OpenAPI:** `http://127.0.0.1:8080/docs`

From the **repo root**:

```bash
docker compose -f astrocyte-services-py/docker-compose.yml --env-file astrocyte-services-py/.env up --build
```

### Production-shaped runbook (migrations + HNSW)

For SQL-owned schema and ANN indexes, use the runbook script and overlays documented in the services README:

```bash
./astrocyte-services-py/scripts/runbook-up.sh
```

Details, troubleshooting, and environment split (**`ASTROCYTES_REST_DATABASE_URL`** vs migrate DSN) are in **[`astrocyte-services-py/README.md`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-services-py/README.md)** on GitHub.

## 4. Next steps

- **Agent + memory patterns/tutorials:** [100 Agents in 100 Days](/tutorials/100-agents-in-100-days/).
- **Operate and harden HTTP:** [Production-grade reference server](/end-user/production-grade-http-service/) (security, auth, grants, observability).
- **Implement a provider or transport:** [Plugin developer docs](/plugins/provider-spi/) (SPI, entry points, packaging).
- **Understand the design:** start with [Neuroscience & vocabulary](/design/neuroscience-astrocyte/) and [Architecture](/design/architecture-framework/).
