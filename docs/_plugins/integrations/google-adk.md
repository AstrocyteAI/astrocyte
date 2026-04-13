# Google ADK integration

Astrocyte memory as async callable tools for Google's Agent Development Kit.

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

## End-to-end example

A research assistant that accumulates knowledge across interactions:

```python
import asyncio
from astrocyte import Astrocyte
from astrocyte.integrations.google_adk import astrocyte_adk_tools
from google.adk import Agent, Runner

brain = Astrocyte.from_config("astrocyte.yaml")

agent = Agent(
    model="gemini-2.0-flash",
    name="research_assistant",
    instruction=(
        "You are a research assistant with persistent memory. "
        "When the user shares findings, store them with memory_retain. "
        "When asked a question, check memory_recall first. "
        "Use memory_reflect to synthesize across all stored findings."
    ),
    tools=astrocyte_adk_tools(brain, bank_id="research-project"),
)

async def main():
    runner = Runner(agent=agent, app_name="research")

    # User shares a finding
    response = await runner.run(
        user_id="researcher-1",
        session_id="s1",
        new_message="According to the 2025 survey, 73% of teams use RAG pipelines.",
    )
    print(response.text)
    # Agent stores the finding via memory_retain

    # Later: ask about stored knowledge
    response = await runner.run(
        user_id="researcher-1",
        session_id="s2",
        new_message="What do we know about RAG adoption?",
    )
    print(response.text)
    # Agent calls memory_recall, finds the survey data

asyncio.run(main())
```

## Tool dispatch detail

ADK auto-generates function schemas from type annotations and docstrings — no manual schema definition needed:

```python
# Each tool function looks like:
async def memory_retain(content: str, tags: str | None = None) -> dict:
    """Store content into persistent memory. Tags are comma-separated."""
    ...
```

## Integration pattern

| Tool function | Parameters | Returns |
|---|---|---|
| `memory_retain(content, tags?)` | `str`, `str \| None` | `dict` with `stored`, `memory_id` |
| `memory_recall(query, max_results?)` | `str`, `int` | `dict` with `hits`, `total` |
| `memory_reflect(query)` | `str` | `dict` with `answer` |
| `memory_forget(memory_ids)` | `str` (comma-separated) | `dict` with `deleted_count` |

## API reference

### `astrocyte_adk_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `list` of async callable functions. Each function has `__name__`, `__doc__`, and type annotations used by ADK for schema generation.
