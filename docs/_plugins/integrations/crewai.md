# CrewAI integration

Astrocytes as shared crew memory and per-agent memory for CrewAI applications.

**Module:** `astrocytes.integrations.crewai`
**Pattern:** Crew/agent memory — save, search, reset, per-agent banks
**Framework dependency:** `crewai` (optional)

## Install

```bash
pip install astrocytes crewai
```

## Usage

```python
from astrocytes import Astrocyte
from astrocytes.integrations.crewai import AstrocyteCrewMemory

brain = Astrocyte.from_config("astrocytes.yaml")

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

| CrewAI operation | Astrocytes call |
|---|---|
| `memory.save(content, agent_id=...)` | `brain.retain()` with agent metadata |
| `memory.search(query, agent_id=...)` | `brain.recall()` scoped to agent or shared bank |
| `memory.reset(agent_id=...)` | `brain.forget(scope="all")` on the agent's bank |

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
