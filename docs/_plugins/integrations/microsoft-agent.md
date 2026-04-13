# Microsoft Agent Framework integration

Astrocyte memory tools for the Microsoft Agent Framework.

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

| Tool | Handler | Astrocyte call |
|---|---|---|
| `memory_retain` | `handlers["memory_retain"]` | `brain.retain()` |
| `memory_recall` | `handlers["memory_recall"]` | `brain.recall()` |
| `memory_reflect` | `handlers["memory_reflect"]` | `brain.reflect()` |
| `memory_forget` | `handlers["memory_forget"]` | `brain.forget()` |

## End-to-end example

A Microsoft Agent with memory for meeting notes:

```python
import asyncio
from astrocyte import Astrocyte
from astrocyte.integrations.microsoft_agent import astrocyte_ms_agent_tools

brain = Astrocyte.from_config("astrocyte.yaml")
tools, handlers = astrocyte_ms_agent_tools(brain, bank_id="meeting-notes")

async def main():
    # Store meeting notes
    result = await handlers["memory_retain"](
        content="Q2 planning: migrate to Kubernetes by July, budget approved for 3 new hires",
    )
    print(result)

    # Recall for a follow-up meeting
    result = await handlers["memory_recall"](query="Q2 planning decisions")
    print(result)

    # Synthesize action items
    result = await handlers["memory_reflect"](query="What are the open action items?")
    print(result)

asyncio.run(main())
```

## Shared format with OpenAI Agents SDK

This integration uses the same OpenAI-compatible function-calling format. If you're already using the [OpenAI Agents SDK](./openai-agents.md) integration, the tool dispatch code is identical.

## API reference

### `astrocyte_ms_agent_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `tuple[list[dict], dict[str, Callable]]` — same signature as `astrocyte_tool_definitions()`.
