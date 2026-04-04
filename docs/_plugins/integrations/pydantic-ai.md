# Pydantic AI integration

Astrocyte memory exposed as tool functions for Pydantic AI agents.

**Module:** `astrocyte.integrations.pydantic_ai`
**Pattern:** Agent tools — retain, recall, reflect as callable tool functions
**Framework dependency:** `pydantic-ai` (optional)

## Install

```bash
pip install astrocyte pydantic-ai
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.pydantic_ai import astrocyte_tools

brain = Astrocyte.from_config("astrocyte.yaml")
tools = astrocyte_tools(brain, bank_id="user-123")

# Register with Pydantic AI agent
from pydantic_ai import Agent
agent = Agent(model="claude-sonnet-4-20250514", tools=tools)

# The agent can now call during execution:
#   memory_retain("Calvin prefers dark mode", tags=["preference"])
#   memory_recall("What are Calvin's preferences?")
#   memory_reflect("Summarize what we know about Calvin")
```

## Integration pattern

| Tool name | What it does | Returns |
|---|---|---|
| `memory_retain(content, tags?)` | `brain.retain()` | `"Stored memory (id: ...)"` |
| `memory_recall(query, max_results?)` | `brain.recall()` | Formatted list of scored hits |
| `memory_reflect(query)` | `brain.reflect()` | Synthesized answer text |
| `memory_forget(memory_ids)` | `brain.forget()` | `"Deleted N memories."` (opt-in) |

## API reference

### `astrocyte_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `list[dict]` where each dict has `name`, `description`, `function`.

| Parameter | Type | Description |
|---|---|---|
| `brain` | `Astrocyte` | Configured Astrocyte instance |
| `bank_id` | `str` | Memory bank for all tool calls |
| `include_reflect` | `bool` | Include reflect tool (default: True) |
| `include_forget` | `bool` | Include forget tool (default: False) |
