# Strands Agents (AWS) integration

Astrocytes memory tools for AWS Strands Agents framework.

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

## API reference

### `astrocyte_strands_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `list[dict]` where each dict has `spec` and `handler`.
