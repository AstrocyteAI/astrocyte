# Claude Managed Agents integration

Astrocyte memory as custom tools for [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) — Anthropic's cloud-hosted agent platform.

**Module:** `astrocyte.integrations.claude_managed_agents`
**Pattern:** Custom tools + SSE event loop
**Framework dependency:** `anthropic` (standard SDK, beta header `managed-agents-2026-04-01`)

## Install

```bash
pip install astrocyte anthropic
```

## How it works

Unlike in-process MCP integrations, Managed Agents uses a REST API with server-sent events. Astrocyte memory operations are defined as **custom tools** on the agent. When the agent calls a memory tool, the session pauses, your application executes the tool via Astrocyte, and sends the result back.

```
Agent calls memory_retain
    -> SSE: agent.custom_tool_use event
    -> SSE: session.status_idle (requires_action)
    -> Your app: handle_memory_tool(brain, "memory_retain", input, bank_id=...)
    -> Your app: sends user.custom_tool_result event
    -> Agent continues
```

## Quick start

```python
from anthropic import Anthropic
from astrocyte import Astrocyte
from astrocyte.integrations.claude_managed_agents import (
    memory_tool_definitions,
    handle_memory_tool,
    is_memory_tool,
)

brain = Astrocyte.from_config("astrocyte.yaml")
client = Anthropic()

# 1. Create agent with Astrocyte memory tools
agent = client.beta.agents.create(
    name="Assistant with memory",
    model="claude-sonnet-4-6",
    system="You are a helpful assistant with long-term memory.",
    tools=[
        {"type": "agent_toolset_20260401"},
        *memory_tool_definitions(),
    ],
)

# 2. Create environment and session
environment = client.beta.environments.create(
    name="my-env",
    config={"type": "cloud", "networking": {"type": "unrestricted"}},
)
session = client.beta.sessions.create(
    agent=agent.id,
    environment_id=environment.id,
)

# 3. Run the event loop
bank_id = "user-123"
events_by_id = {}

with client.beta.sessions.events.stream(session.id) as stream:
    client.beta.sessions.events.send(
        session.id,
        events=[{
            "type": "user.message",
            "content": [{"type": "text", "text": "What do you remember about me?"}],
        }],
    )

    for event in stream:
        if event.type == "agent.message":
            for block in event.content:
                print(block.text, end="")

        elif event.type == "agent.custom_tool_use":
            events_by_id[event.id] = event

        elif event.type == "session.status_idle":
            stop = event.stop_reason
            if stop and stop.type == "requires_action":
                for event_id in stop.event_ids:
                    tool_event = events_by_id[event_id]
                    if is_memory_tool(tool_event.name):
                        import asyncio
                        result = asyncio.run(handle_memory_tool(
                            brain, tool_event.name, tool_event.input,
                            bank_id=bank_id,
                        ))
                        client.beta.sessions.events.send(
                            session.id,
                            events=[{
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": event_id,
                                "content": [{"type": "text", "text": result}],
                            }],
                        )
            elif stop and stop.type == "end_turn":
                break
```

## High-level helper

For simpler use cases, `run_session_with_memory` handles the entire event loop:

```python
from astrocyte.integrations.claude_managed_agents import run_session_with_memory

result = await run_session_with_memory(
    client, brain,
    session_id=session.id,
    prompt="Store this: I prefer dark mode. Then recall my preferences.",
    bank_id="user-123",
)
print(result)
```

## Tools provided

| Tool | Description | Returns |
|---|---|---|
| `memory_retain` | Store content into long-term memory | `{"stored": true, "memory_id": "..."}` |
| `memory_recall` | Search memory by relevance | `{"hits": [...], "total": N}` |
| `memory_reflect` | Synthesize a narrative answer from memory | Plain text answer |
| `memory_forget` | Delete specific memories by ID | `{"deleted_count": N}` |

## Astrocyte vs. built-in memory_stores

Managed Agents has its own [built-in memory_stores](https://platform.claude.com/docs/en/managed-agents/memory) API (research preview). Here's when to use each:

| Feature | Astrocyte | Built-in memory_stores |
|---|---|---|
| Semantic search | Yes (vector similarity) | Full-text search |
| Reflect (LLM synthesis) | Yes | No |
| PII barriers | Yes | No |
| Multi-bank isolation | Yes | Per-store isolation |
| Deduplication | Yes (configurable) | No |
| Compliance rules | Yes (MIP) | No |
| Path-based organization | No | Yes |
| Version history | No | Yes |
| Zero-code setup | No | Yes (attach to session) |

Use Astrocyte when you need semantic search, compliance guardrails, or cross-framework portability. Use built-in memory_stores for simple document storage with versioning.

## API reference

### `memory_tool_definitions(*, include_reflect=True, include_forget=False)`

Returns `list[dict]` of custom tool definitions for `client.beta.agents.create(tools=...)`. Each dict has `type: "custom"`, `name`, `description`, `input_schema`.

### `handle_memory_tool(brain, tool_name, tool_input, *, bank_id)`

Async function that executes an Astrocyte memory operation and returns the result as a string. Call this from your event loop when you receive an `agent.custom_tool_use` event.

### `is_memory_tool(name)`

Returns `True` if `name` is one of the four Astrocyte memory tool names.

### `run_session_with_memory(client, brain, *, session_id, prompt, bank_id, non_memory_tool_handler=None, timeout_seconds=120)`

Async high-level helper that runs a full session turn: opens stream, sends prompt, handles memory tool calls, returns concatenated agent text.
