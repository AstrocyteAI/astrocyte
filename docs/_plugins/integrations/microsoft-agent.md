# Microsoft Agent Framework integration

Astrocytes memory tools for the Microsoft Agent Framework.

**Module:** `astrocyte.integrations.microsoft_agent`
**Pattern:** OpenAI-compatible tool definitions + handler dispatch
**Framework dependency:** `microsoft-agent-framework` (optional)

## Install

```bash
pip install astrocyte microsoft-agent-framework
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.microsoft_agent import astrocyte_ms_agent_tools

brain = Astrocyte.from_config("astrocyte.yaml")
tools, handlers = astrocyte_ms_agent_tools(brain, bank_id="user-123")

from microsoft.agent_framework import Agent
agent = Agent(tools=tools)

# Dispatch tool calls
result = await handlers["memory_retain"](content="Calvin prefers dark mode")
result = await handlers["memory_recall"](query="preferences")
```

## Integration pattern

Uses the same OpenAI-compatible format as the OpenAI Agents SDK integration. See [OpenAI Agents SDK](./openai-agents.md) for the full tool schema format.

| Tool | Handler | Astrocytes call |
|---|---|---|
| `memory_retain` | `handlers["memory_retain"]` | `brain.retain()` |
| `memory_recall` | `handlers["memory_recall"]` | `brain.recall()` |
| `memory_reflect` | `handlers["memory_reflect"]` | `brain.reflect()` |
| `memory_forget` | `handlers["memory_forget"]` | `brain.forget()` |

## API reference

### `astrocyte_ms_agent_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `tuple[list[dict], dict[str, Callable]]` — same signature as `astrocyte_tool_definitions()`.
