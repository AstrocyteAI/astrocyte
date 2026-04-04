# AutoGen / AG2 integration

Astrocytes memory for Microsoft AutoGen multi-agent conversations, with both direct API and tool registration.

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

| Method | Astrocytes call |
|---|---|
| `save(content, agent_id=...)` | `brain.retain()` with agent metadata |
| `query(query, agent_id=...)` | `brain.recall()` → list of hit dicts |
| `get_context(query)` | `brain.recall()` → formatted string |
| `as_tools()` | OpenAI-format tool definitions |
| `get_handlers()` | Async handler functions for tool dispatch |

## API reference

### `AstrocyteAutoGenMemory(brain, bank_id, *, agent_banks=None)`

| Parameter | Type | Description |
|---|---|---|
| `brain` | `Astrocyte` | Configured Astrocyte instance |
| `bank_id` | `str` | Shared team bank |
| `agent_banks` | `dict[str, str]` | Agent ID → bank ID for per-agent memory |
