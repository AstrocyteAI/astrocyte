# BeeAI (IBM) integration

Astrocyte memory tools for IBM's BeeAI Agent Framework.

**Module:** `astrocyte.integrations.beeai`
**Pattern:** Tool with `run()` method — name, description, input_schema, async run()
**Framework dependency:** `beeai-framework` (optional)

## Install

```bash
pip install astrocyte beeai-framework
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.beeai import astrocyte_bee_tools

brain = Astrocyte.from_config("astrocyte.yaml")
tools = astrocyte_bee_tools(brain, bank_id="user-123")

from beeai import BeeAgent
agent = BeeAgent(llm=llm, tools=tools)

# Or call tools directly:
result = await tools[0].run({"content": "Calvin likes Python"})
```

## Integration pattern

Each tool is an `AstrocyteBeeTool` with `name`, `description`, `input_schema`, and async `run()`:

| Tool | Input | Returns |
|---|---|---|
| `memory_retain` | `{"content": "..."}` | JSON: `{"stored": true, "memory_id": "..."}` |
| `memory_recall` | `{"query": "..."}` | JSON: `{"hits": [...], "total": N}` |
| `memory_reflect` | `{"query": "..."}` | Answer text |
| `memory_forget` | `{"memory_ids": [...]}` | JSON: `{"deleted_count": N}` |

## End-to-end example

A BeeAI agent that tracks customer interactions:

```python
import asyncio
from astrocyte import Astrocyte
from astrocyte.integrations.beeai import astrocyte_bee_tools
from beeai import BeeAgent

brain = Astrocyte.from_config("astrocyte.yaml")
tools = astrocyte_bee_tools(brain, bank_id="customer-interactions")

agent = BeeAgent(
    llm=llm,
    tools=tools,
    instructions=(
        "You are a customer success agent. Store key customer details and "
        "interactions with memory_retain. Before responding to a customer, "
        "use memory_recall to retrieve their history."
    ),
)

async def main():
    # Store a customer interaction
    result = await tools[0].run({
        "content": "Customer Acme Corp renewed their enterprise plan for 2 years"
    })
    print(result)  # {"stored": true, "memory_id": "..."}

    # Recall customer history
    result = await tools[1].run({
        "query": "Acme Corp",
        "max_results": 5,
    })
    print(result)  # {"hits": [...], "total": 1}

asyncio.run(main())
```

## API reference

### `astrocyte_bee_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `list[AstrocyteBeeTool]`. Each tool has `name`, `description`, `input_schema`, and `run(input_data)`.
