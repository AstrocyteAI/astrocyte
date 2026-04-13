# Smolagents (HuggingFace) integration

Astrocyte memory as code-centric tools for HuggingFace Smolagents.

**Module:** `astrocyte.integrations.smolagents`
**Pattern:** Tool protocol — name, description, inputs schema, output_type, forward()
**Framework dependency:** `smolagents` (optional)

## Install

```bash
pip install astrocyte smolagents
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.smolagents import astrocyte_smolagent_tools

brain = Astrocyte.from_config("astrocyte.yaml")
tools = astrocyte_smolagent_tools(brain, bank_id="user-123")

from smolagents import CodeAgent, HfApiModel
agent = CodeAgent(tools=tools, model=HfApiModel())

# The agent writes Python code that calls:
#   memory_retain(content="...", tags="pref,ui")
#   memory_recall(query="...", max_results=5)
#   memory_reflect(query="...")
```

## Integration pattern

Each tool is an `AstrocyteSmolTool` instance implementing the smolagents Tool protocol:

| Field | Description |
|---|---|
| `name` | Tool identifier (e.g., `"memory_retain"`) |
| `description` | What the tool does (used by the agent) |
| `inputs` | Dict of parameter schemas |
| `output_type` | Return type (`"string"`) |
| `forward(**kwargs)` | Async execution method |
| `__call__(**kwargs)` | Sync fallback |

## End-to-end example

A code-generating agent that remembers past solutions:

```python
from astrocyte import Astrocyte
from astrocyte.integrations.smolagents import astrocyte_smolagent_tools
from smolagents import CodeAgent, HfApiModel

brain = Astrocyte.from_config("astrocyte.yaml")
tools = astrocyte_smolagent_tools(brain, bank_id="code-solutions")

agent = CodeAgent(
    tools=tools,
    model=HfApiModel(),
    system_prompt=(
        "You write Python code. Before solving a problem, call "
        "memory_recall to check for similar past solutions. After solving, "
        "store the solution with memory_retain for future reference."
    ),
)

# The agent writes Python code that calls tools directly:
# result = memory_recall(query="sorting algorithm", max_results=3)
# memory_retain(content="Used merge sort for large lists", tags="algorithm,sorting")

result = agent.run("Write a function to sort a list of 1M integers efficiently")
```

## Sync and async

Smolagents tools support both sync and async execution:

```python
tool = tools[0]  # memory_retain

# Async (preferred)
result = await tool.forward(content="test memory")

# Sync fallback (blocks event loop)
result = tool(content="test memory")
```

## API reference

### `astrocyte_smolagent_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `list[AstrocyteSmolTool]`.
