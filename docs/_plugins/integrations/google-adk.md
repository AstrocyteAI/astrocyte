# Google ADK integration

Astrocytes memory as async callable tools for Google's Agent Development Kit.

**Module:** `astrocyte.integrations.google_adk`
**Pattern:** Async callable functions with type annotations for ADK schema generation
**Framework dependency:** `google-adk` (optional)

## Install

```bash
pip install astrocyte google-adk
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.google_adk import astrocyte_adk_tools

brain = Astrocyte.from_config("astrocyte.yaml")
tools = astrocyte_adk_tools(brain, bank_id="user-123")

from google.adk import Agent
agent = Agent(model="gemini-2.0-flash", tools=tools)
```

## Integration pattern

Each tool is an async function with type annotations and a docstring — ADK uses these for automatic schema generation.

| Tool function | Parameters | Returns |
|---|---|---|
| `memory_retain(content, tags?)` | `str`, `str \| None` | `dict` with `stored`, `memory_id` |
| `memory_recall(query, max_results?)` | `str`, `int` | `dict` with `hits`, `total` |
| `memory_reflect(query)` | `str` | `dict` with `answer` |
| `memory_forget(memory_ids)` | `str` (comma-separated) | `dict` with `deleted_count` |

## API reference

### `astrocyte_adk_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `list` of async callable functions. Each function has `__name__`, `__doc__`, and type annotations used by ADK for schema generation.
