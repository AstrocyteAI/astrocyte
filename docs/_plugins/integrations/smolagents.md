# Smolagents (HuggingFace) integration

Astrocytes memory as code-centric tools for HuggingFace Smolagents.

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

## API reference

### `astrocyte_smolagent_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `list[AstrocyteSmolTool]`.
