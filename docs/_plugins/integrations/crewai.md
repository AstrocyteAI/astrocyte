# CrewAI integration

Astrocyte as shared crew memory and per-agent memory for CrewAI applications.

**Module:** `astrocyte.integrations.crewai`
**Pattern:** Crew/agent memory — save, search, reset, per-agent banks
**Framework dependency:** `crewai` (optional)

## Install

```bash
pip install astrocyte crewai
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.crewai import AstrocyteCrewMemory

brain = Astrocyte.from_config("astrocyte.yaml")

# Shared crew memory + per-agent banks
memory = AstrocyteCrewMemory(
    brain,
    bank_id="team-shared",
    agent_banks={"devops": "devops-bank", "researcher": "research-bank"},
)

# Save with agent attribution
await memory.save(
    "Kubernetes deploys via Helm charts",
    agent_id="devops",
    tags=["infrastructure"],
)

# Search across the crew's shared bank
results = await memory.search("deployment strategy")

# Reset a specific agent's memory
await memory.reset(agent_id="devops")
```

## Integration pattern

| CrewAI operation | Astrocyte call |
|---|---|
| `memory.save(content, agent_id=...)` | `brain.retain()` with agent metadata |
| `memory.search(query, agent_id=...)` | `brain.recall()` scoped to agent or shared bank |
| `memory.reset(agent_id=...)` | `brain.clear_bank()` on the agent's bank |

## End-to-end example

A two-agent crew where a researcher stores findings and a writer retrieves them:

```python
import asyncio
from astrocyte import Astrocyte
from astrocyte.integrations.crewai import AstrocyteCrewMemory

brain = Astrocyte.from_config("astrocyte.yaml")

memory = AstrocyteCrewMemory(
    brain,
    bank_id="project-shared",
    agent_banks={
        "researcher": "research-findings",
        "writer": "draft-notes",
    },
)

async def run_crew():
    # Researcher stores findings in their bank
    await memory.save(
        "Market size for AI agents is projected at $47B by 2030",
        agent_id="researcher",
        tags=["market-research", "stats"],
    )
    await memory.save(
        "Top 3 competitors: LangMem, Mem0, Zep",
        agent_id="researcher",
        tags=["market-research", "competitors"],
    )

    # Writer searches the researcher's findings
    findings = await memory.search(
        "market size and competition",
        agent_id="researcher",
        max_results=5,
    )
    for hit in findings:
        print(f"  [{hit['score']:.2f}] {hit['text']}")

    # Writer also stores to shared bank (no agent_id)
    await memory.save(
        "Draft intro: The AI agent market is booming...",
        tags=["draft"],
    )

    # Search across the shared bank
    shared = await memory.search("AI agent market")
    print(f"Shared memories: {len(shared)}")

asyncio.run(run_crew())
```

## API reference

### `AstrocyteCrewMemory(brain, bank_id, *, agent_banks=None, auto_retain=False)`

| Parameter | Type | Description |
|---|---|---|
| `brain` | `Astrocyte` | Configured Astrocyte instance |
| `bank_id` | `str` | Shared crew bank |
| `agent_banks` | `dict[str, str]` | Agent ID → bank ID mapping for per-agent memory |
| `auto_retain` | `bool` | Auto-retain after each task (default: False) |

### Methods

- `save(content, *, agent_id=None, tags=None, metadata=None)` → `None`
- `search(query, *, agent_id=None, max_results=5, tags=None)` → `list[dict]`
- `reset(*, agent_id=None)` → `None`
