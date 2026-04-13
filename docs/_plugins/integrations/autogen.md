# AutoGen / AG2 integration

Astrocyte memory for Microsoft AutoGen multi-agent conversations, with both direct API and tool registration.

**Module:** `astrocyte.integrations.autogen`
**Pattern:** Memory API (save/query/get_context) + OpenAI-format tool export
**Framework dependency:** `ag2` (optional)

## Install

```bash
pip install astrocyte ag2
```

## Usage — Direct memory API

```python
from astrocyte import Astrocyte
from astrocyte.integrations.autogen import AstrocyteAutoGenMemory

brain = Astrocyte.from_config("astrocyte.yaml")
memory = AstrocyteAutoGenMemory(
    brain,
    bank_id="team",
    agent_banks={"agent-a": "bank-a", "agent-b": "bank-b"},
)

# Save in conversation hooks
await memory.save("User prefers Python", agent_id="agent-a")

# Get formatted context for injection
context = await memory.get_context("What does the user prefer?")
# → "- User prefers Python"
```

## Usage — Tool registration

```python
# Export as OpenAI-format tools for function calling
tools = memory.as_tools()
handlers = memory.get_handlers()

# Register with ConversableAgent
agent = ConversableAgent("assistant", llm_config=llm_config)
```

## Integration pattern

| Method | Astrocyte call |
|---|---|
| `save(content, agent_id=...)` | `brain.retain()` with agent metadata |
| `query(query, agent_id=...)` | `brain.recall()` → list of hit dicts |
| `get_context(query)` | `brain.recall()` → formatted string |
| `as_tools()` | OpenAI-format tool definitions |
| `get_handlers()` | Async handler functions for tool dispatch |

## End-to-end example

A multi-agent AutoGen conversation with shared memory:

```python
import asyncio
from astrocyte import Astrocyte
from astrocyte.integrations.autogen import AstrocyteAutoGenMemory
from autogen import ConversableAgent

brain = Astrocyte.from_config("astrocyte.yaml")

memory = AstrocyteAutoGenMemory(
    brain,
    bank_id="team-shared",
    agent_banks={"coder": "coder-notes", "reviewer": "review-notes"},
)

async def main():
    # Coder stores implementation notes
    await memory.save(
        "Implemented retry logic with exponential backoff, max 3 retries",
        agent_id="coder",
    )

    # Reviewer retrieves coder's notes for review context
    context = await memory.get_context("retry implementation details")
    print(context)

    # Both agents share the team bank
    await memory.save("Sprint goal: complete auth module by Friday")
    results = await memory.query("sprint goals")
    for hit in results:
        print(f"  {hit['text']}")

    # Export as OpenAI-format tools for function calling
    tools = memory.as_tools()
    handlers = memory.get_handlers()

    coder = ConversableAgent(
        "coder",
        llm_config={"tools": tools, **llm_config},
    )

asyncio.run(main())
```

## API reference

### `AstrocyteAutoGenMemory(brain, bank_id, *, agent_banks=None)`

| Parameter | Type | Description |
|---|---|---|
| `brain` | `Astrocyte` | Configured Astrocyte instance |
| `bank_id` | `str` | Shared team bank |
| `agent_banks` | `dict[str, str]` | Agent ID → bank ID for per-agent memory |
