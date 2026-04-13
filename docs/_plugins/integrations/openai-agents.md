# OpenAI Agents SDK integration

Astrocyte memory as OpenAI function-calling tool definitions with async handler dispatch.

**Module:** `astrocyte.integrations.openai_agents`
**Pattern:** Function calling — OpenAI-format tool schemas + handler functions
**Framework dependency:** None (produces plain dicts)

## Install

```bash
pip install astrocyte openai
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.openai_agents import astrocyte_tool_definitions
import json

brain = Astrocyte.from_config("astrocyte.yaml")
tools, handlers = astrocyte_tool_definitions(brain, bank_id="user-123")

# Pass tools to OpenAI API
response = client.chat.completions.create(
    model="gpt-4o",
    tools=tools,
    messages=[{"role": "user", "content": "What do you remember about my preferences?"}],
)

# Dispatch tool calls
for call in response.choices[0].message.tool_calls:
    handler = handlers[call.function.name]
    result = await handler(**json.loads(call.function.arguments))
```

## Integration pattern

| Tool name | OpenAI function | Astrocyte call |
|---|---|---|
| `memory_retain` | `{"content": "...", "tags": [...]}` | `brain.retain()` |
| `memory_recall` | `{"query": "...", "max_results": 5}` | `brain.recall()` |
| `memory_reflect` | `{"query": "..."}` | `brain.reflect()` |
| `memory_forget` | `{"memory_ids": [...]}` | `brain.forget()` |

## End-to-end example

A coding assistant with memory across chat sessions:

```python
import asyncio
import json
from openai import AsyncOpenAI
from astrocyte import Astrocyte
from astrocyte.integrations.openai_agents import astrocyte_tool_definitions

brain = Astrocyte.from_config("astrocyte.yaml")
client = AsyncOpenAI()

tools, handlers = astrocyte_tool_definitions(brain, bank_id="user-123")

async def chat(user_message: str):
    messages = [
        {"role": "system", "content": (
            "You have persistent memory. Use memory_recall before answering "
            "to check what you know. Use memory_retain to save important facts."
        )},
        {"role": "user", "content": user_message},
    ]

    # Loop to handle tool calls
    while True:
        response = await client.chat.completions.create(
            model="gpt-4o", messages=messages, tools=tools,
        )
        choice = response.choices[0]

        if choice.finish_reason == "tool_calls":
            messages.append(choice.message)
            for call in choice.message.tool_calls:
                handler = handlers[call.function.name]
                result = await handler(**json.loads(call.function.arguments))
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })
        else:
            return choice.message.content

async def main():
    # Session 1
    print(await chat("I'm working on a FastAPI project with PostgreSQL."))
    # Session 2 — agent recalls the project context
    print(await chat("What database am I using?"))

asyncio.run(main())
```

## API reference

### `astrocyte_tool_definitions(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `tuple[list[dict], dict[str, Callable]]`:
- **tools**: list of OpenAI function-calling format dicts
- **handlers**: dict mapping tool name → async handler function

Handlers accept keyword arguments matching the tool's parameter schema and return JSON strings.
