# Claude Agent SDK integration

Native Astrocyte memory tools for the Anthropic Claude Agent SDK, using the `@tool` decorator and in-process MCP servers.

**Module:** `astrocyte.integrations.claude_agent_sdk`
**Pattern:** Native `@tool` + `create_sdk_mcp_server` (in-process MCP)
**Framework dependency:** `claude-agent-sdk` (optional for `astrocyte_claude_agent_server()`)

## Install

```bash
pip install astrocyte claude-agent-sdk
```

## Usage — Native SDK server

```python
from astrocyte import Astrocyte
from astrocyte.integrations.claude_agent_sdk import astrocyte_claude_agent_server

brain = Astrocyte.from_config("astrocyte.yaml")
memory_server = astrocyte_claude_agent_server(brain, bank_id="user-123")

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

options = ClaudeAgentOptions(
    mcp_servers={"memory": memory_server},
    allowed_tools=["mcp__astrocyte_memory__*"],
)

async for message in query(
    prompt="What do you remember about my preferences?",
    options=options,
):
    if isinstance(message, ResultMessage) and message.subtype == "success":
        print(message.result)
```

## Usage — Without SDK dependency (testing)

```python
from astrocyte.integrations.claude_agent_sdk import astrocyte_claude_agent_tools

tools = astrocyte_claude_agent_tools(brain, bank_id="user-123")

# Each tool returns SDK format: {"content": [{"type": "text", "text": "..."}]}
result = await tools[0]["handler"]({"content": "Calvin prefers dark mode"})
```

## Integration pattern

| Tool | SDK name | Returns |
|---|---|---|
| `memory_retain` | `mcp__astrocyte_memory__memory_retain` | `{"content": [{"type": "text", "text": '{"stored": true}'}]}` |
| `memory_recall` | `mcp__astrocyte_memory__memory_recall` | `{"content": [{"type": "text", "text": '{"hits": [...]}'}]}` |
| `memory_reflect` | `mcp__astrocyte_memory__memory_reflect` | `{"content": [{"type": "text", "text": "answer text"}]}` |
| `memory_forget` | `mcp__astrocyte_memory__memory_forget` | `{"content": [{"type": "text", "text": '{"deleted_count": N}'}]}` |

## End-to-end example

A Claude agent that builds up project knowledge across sessions:

```python
import asyncio
from astrocyte import Astrocyte
from astrocyte.integrations.claude_agent_sdk import astrocyte_claude_agent_server
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

brain = Astrocyte.from_config("astrocyte.yaml")

options = ClaudeAgentOptions(
    mcp_servers={
        "memory": astrocyte_claude_agent_server(brain, bank_id="project-alpha"),
    },
    allowed_tools=["mcp__astrocyte_memory__*"],
    system=(
        "You have persistent memory via MCP tools. "
        "Store important project decisions with memory_retain. "
        "Check memory_recall before answering project questions."
    ),
)

async def chat(prompt: str) -> str:
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage) and message.subtype == "success":
            return message.result
    return ""

async def main():
    # Session 1: share project decisions
    print(await chat("We decided to use PostgreSQL with pgvector for storage."))

    # Session 2: agent recalls without being told
    print(await chat("What database are we using for this project?"))

asyncio.run(main())
```

## Multi-agent with session-scoped memory

Use `create_subagent_definition` for multi-agent setups where each sub-agent has isolated memory but can read from the coordinator:

```python
from astrocyte.integrations.claude_agent_sdk import (
    create_coordinator_server,
    create_subagent_definition,
)

coordinator = create_coordinator_server(brain, session_id="sess-1")
researcher = create_subagent_definition(brain, role="researcher", session_id="sess-1")
writer = create_subagent_definition(brain, role="writer", session_id="sess-1")
```

## Claude Agent SDK helpers

For session-scoped memory with the Claude Agent SDK (the local CLI-based SDK), use the helpers in `astrocyte.integrations.managed_agents`:

- `create_memory_server(brain, session_id=...)` — session-scoped memory bank
- `create_coordinator_server(brain, session_id=...)` — multi-agent coordinator with cross-bank recall
- `create_subagent_definition(brain, role=..., session_id=...)` — sub-agent with isolated bank + coordinator read access

## API reference

### `astrocyte_claude_agent_server(brain, bank_id, *, server_name="astrocyte_memory", include_reflect=True, include_forget=False)`

Returns an SDK MCP server. Requires `claude-agent-sdk` installed.

### `astrocyte_claude_agent_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `list[dict]` with `name`, `description`, `input_schema`, `handler`. No SDK dependency.
