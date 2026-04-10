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

## Managed Agents

For cloud-hosted agents using [Claude Managed Agents](https://claude.com/blog/claude-managed-agents), use the session-aware helpers in `astrocyte.integrations.managed_agents`.

Managed Agents handles orchestration and sandboxing. Astrocyte handles what the agent remembers, in which bank, under what compliance rules.

### Single agent — session-scoped memory

Each SDK session gets its own isolated memory bank automatically:

```python
from astrocyte import Astrocyte
from astrocyte.integrations.managed_agents import create_memory_server

brain = Astrocyte.from_config("astrocyte.yaml")
server = create_memory_server(brain, session_id="sess-abc123")

options = ClaudeAgentOptions(
    mcp_servers={"memory": server},
    allowed_tools=["mcp__astrocyte_memory__*"],
)
```

### Multi-agent — coordinated memory banks

A coordinator agent shares a bank namespace with sub-agents. Each sub-agent writes to its own bank; all agents can read from the shared coordinator bank.

```
session-{id}:coordinator        <- shared, all agents read
session-{id}:agent-researcher   <- researcher writes here
session-{id}:agent-analyst      <- analyst writes here
```

```python
from astrocyte.integrations.managed_agents import (
    create_coordinator_server,
    create_subagent_definition,
)

brain = Astrocyte.from_config("astrocyte.yaml")
coord_server = create_coordinator_server(brain, session_id="sess-abc")

researcher = create_subagent_definition(
    brain,
    role="researcher",
    session_id="sess-abc",
    description="Research agent that explores files and web sources.",
    prompt="You are a research specialist. Store findings in memory.",
    tools=["Read", "Grep", "Glob", "WebSearch", "WebFetch"],
)

options = ClaudeAgentOptions(
    mcp_servers={"memory": coord_server},
    allowed_tools=["mcp__memory__*", "Agent"],
    agents={"researcher": AgentDefinition(**researcher)},
)
```

The coordinator can query across all sub-agent banks using the `include_agents` parameter on `memory_recall` and `memory_reflect`. Sub-agents can only read their own bank and the coordinator bank — they cannot see other agents' memory.

### Remote MCP server

For zero-code integration, run Astrocyte as an SSE server and connect from any Managed Agent:

```bash
astrocyte-mcp --config astrocyte.yaml --transport sse --port 8080
```

```python
options = ClaudeAgentOptions(
    mcp_servers={
        "memory": {"type": "sse", "url": "http://localhost:8080/mcp/sse"}
    },
    allowed_tools=["mcp__memory__*"],
)
```

## API reference

### `astrocyte_claude_agent_server(brain, bank_id, *, server_name="astrocyte_memory", include_reflect=True, include_forget=False)`

Returns an SDK MCP server. Requires `claude-agent-sdk` installed.

### `astrocyte_claude_agent_tools(brain, bank_id, *, include_reflect=True, include_forget=False)`

Returns `list[dict]` with `name`, `description`, `input_schema`, `handler`. No SDK dependency.

### `create_memory_server(brain, *, session_id, server_name="astrocyte_memory", include_reflect=True, include_forget=False)`

Session-scoped wrapper around `astrocyte_claude_agent_server`. Bank is `session-{session_id}`.

### `create_coordinator_server(brain, *, session_id, server_name="astrocyte_memory", include_reflect=True)`

MCP server for coordinator agent with multi-bank recall. Tools: `memory_retain`, `memory_recall` (with `include_agents`), `memory_reflect`.

### `create_subagent_definition(brain, *, role, session_id, description, prompt, tools=None, model=None)`

Returns a dict compatible with `ClaudeAgentOptions.agents`. Wires the sub-agent with its own memory bank + coordinator read access.
