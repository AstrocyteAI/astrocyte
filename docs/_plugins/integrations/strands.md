# Strands Agents (AWS) integration

Astrocyte memory tools for AWS Strands Agents framework.

**Module:** `astrocyte.integrations.strands`
**Pattern:** Spec + handler — JSON Schema tool spec with async handler functions
**Framework dependency:** `strands-agents` (optional)

## Install

```bash
pip install astrocyte strands-agents
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.strands import astrocyte_strands_tools

brain = Astrocyte.from_config("astrocyte.yaml")
tools = astrocyte_strands_tools(brain, bank_id="user-123")

from strands import Agent
agent = Agent(model=model, tools=tools)

# Or dispatch handlers directly:
for tool in tools:
    print(tool["spec"]["name"])
result = await tools[0]["handler"]({"content": "test memory"})
```

## Integration pattern

Each tool is a dict with `spec` (JSON Schema) and `handler` (async callable):

| Tool | Handler input | Returns |
|---|---|---|
| `memory_retain` | `{"content": "..."}` | JSON: `{"stored": true, "memory_id": "..."}` |
| `memory_recall` | `{"query": "..."}` | JSON: `{"hits": [...], "total": N}` |
| `memory_reflect` | `{"query": "..."}` | JSON: `{"answer": "..."}` |
| `memory_forget` | `{"memory_ids": [...]}` | JSON: `{"deleted_count": N}` |

## End-to-end example

An AWS Strands agent with persistent memory for customer support:

```python
import asyncio
from astrocyte import Astrocyte
from astrocyte.integrations.strands import astrocyte_strands_tools
from strands import Agent

brain = Astrocyte.from_config("astrocyte.yaml")
tools = astrocyte_strands_tools(brain, bank_id="support-tickets")

agent = Agent(
    model=model,
    tools=tools,
    system_prompt=(
        "You are a customer support agent. Store ticket resolutions with "
        "memory_retain. Check memory_recall for similar past tickets before "
        "suggesting solutions."
    ),
)

async def main():
    # First ticket
    response = agent("Customer can't log in after password reset. "
                      "Solution: clear browser cache and retry.")
    # Agent stores the resolution

    # Similar ticket later
    response = agent("Another user reports login issues after changing password.")
    # Agent recalls the previous resolution and suggests the same fix

asyncio.run(main())
```

## Direct handler dispatch

Each tool is a `{"spec": ..., "handler": ...}` dict — call handlers directly for programmatic use:

```python
tools = astrocyte_strands_tools(brain, bank_id="test")

# Store
result = await tools[0]["handler"]({"content": "test memory", "tags": "test"})

# Recall
result = await tools[1]["handler"]({"query": "test", "max_results": 3})
```

## API reference

### `astrocyte_strands_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `list[dict]` where each dict has `spec` and `handler`.
