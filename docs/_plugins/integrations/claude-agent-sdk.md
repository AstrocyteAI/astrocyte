# Claude Agent SDK integration

Native Astrocytes memory tools for the Anthropic Claude Agent SDK, using the `@tool` decorator and in-process MCP servers.

**Module:** `astrocytes.integrations.claude_agent_sdk`
**Pattern:** Native `@tool` + `create_sdk_mcp_server` (in-process MCP)
**Framework dependency:** `claude-agent-sdk` (optional for `astrocyte_claude_agent_server()`)

## Install

```bash
pip install astrocytes claude-agent-sdk
```

## Usage — Native SDK server

```python
from astrocytes import Astrocyte
from astrocytes.integrations.claude_agent_sdk import astrocyte_claude_agent_server

brain = Astrocyte.from_config("astrocytes.yaml")
memory_server = astrocyte_claude_agent_server(brain, bank_id="user-123")

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

options = ClaudeAgentOptions(
    mcp_servers={"memory": memory_server},
    allowed_tools=["mcp__astrocytes_memory__*"],
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
from astrocytes.integrations.claude_agent_sdk import astrocyte_claude_agent_tools

tools = astrocyte_claude_agent_tools(brain, bank_id="user-123")

# Each tool returns SDK format: {"content": [{"type": "text", "text": "..."}]}
result = await tools[0]["handler"]({"content": "Calvin prefers dark mode"})
```

## Integration pattern

| Tool | SDK name | Returns |
|---|---|---|
| `memory_retain` | `mcp__astrocytes_memory__memory_retain` | `{"content": [{"type": "text", "text": '{"stored": true}'}]}` |
| `memory_recall` | `mcp__astrocytes_memory__memory_recall` | `{"content": [{"type": "text", "text": '{"hits": [...]}'}]}` |
| `memory_reflect` | `mcp__astrocytes_memory__memory_reflect` | `{"content": [{"type": "text", "text": "answer text"}]}` |
| `memory_forget` | `mcp__astrocytes_memory__memory_forget` | `{"content": [{"type": "text", "text": '{"deleted_count": N}'}]}` |

## API reference

### `astrocyte_claude_agent_server(brain, bank_id, *, server_name="astrocytes_memory", include_reflect=True, include_forget=False)`

Returns an SDK MCP server. Requires `claude-agent-sdk` installed.

### `astrocyte_claude_agent_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `list[dict]` with `name`, `description`, `input_schema`, `handler`. No SDK dependency.
