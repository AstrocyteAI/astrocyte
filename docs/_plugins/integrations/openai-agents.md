# OpenAI Agents SDK integration

Astrocytes memory as OpenAI function-calling tool definitions with async handler dispatch.

**Module:** `astrocytes.integrations.openai_agents`
**Pattern:** Function calling — OpenAI-format tool schemas + handler functions
**Framework dependency:** None (produces plain dicts)

## Install

```bash
pip install astrocytes openai
```

## Usage

```python
from astrocytes import Astrocyte
from astrocytes.integrations.openai_agents import astrocyte_tool_definitions
import json

brain = Astrocyte.from_config("astrocytes.yaml")
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

| Tool name | OpenAI function | Astrocytes call |
|---|---|---|
| `memory_retain` | `{"content": "...", "tags": [...]}` | `brain.retain()` |
| `memory_recall` | `{"query": "...", "max_results": 5}` | `brain.recall()` |
| `memory_reflect` | `{"query": "..."}` | `brain.reflect()` |
| `memory_forget` | `{"memory_ids": [...]}` | `brain.forget()` |

## API reference

### `astrocyte_tool_definitions(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `tuple[list[dict], dict[str, Callable]]`:
- **tools**: list of OpenAI function-calling format dicts
- **handlers**: dict mapping tool name → async handler function

Handlers accept keyword arguments matching the tool's parameter schema and return JSON strings.
