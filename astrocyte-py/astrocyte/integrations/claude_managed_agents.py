"""Claude Managed Agents integration — Astrocyte memory as custom tools.

Claude Managed Agents (https://platform.claude.com/docs/en/managed-agents/overview)
is Anthropic's cloud-hosted agent platform. It uses the standard ``anthropic``
SDK with the ``managed-agents-2026-04-01`` beta header.

This module provides helpers for wiring Astrocyte memory operations as
custom tools on a Managed Agent. Custom tools are defined at agent creation
and executed by YOUR application via the SSE event loop — the agent emits
``agent.custom_tool_use`` events, you call Astrocyte, and send back
``user.custom_tool_result`` events.

Integration pattern:
    1. Call ``memory_tool_definitions()`` to get tool dicts for agent creation.
    2. Create the agent with these tools via ``client.beta.agents.create(tools=...)``.
    3. In your event loop, call ``handle_memory_tool()`` when you see an
       ``agent.custom_tool_use`` event for a memory tool.
    4. Send the result back as a ``user.custom_tool_result`` event.

Usage — agent creation:
    from anthropic import Anthropic
    from astrocyte import Astrocyte
    from astrocyte.integrations.claude_managed_agents import (
        memory_tool_definitions,
        handle_memory_tool,
        is_memory_tool,
        run_session_with_memory,
    )

    brain = Astrocyte.from_config("astrocyte.yaml")
    client = Anthropic()

    agent = client.beta.agents.create(
        name="Assistant with memory",
        model="claude-sonnet-4-6",
        system="You are a helpful assistant with long-term memory.",
        tools=[
            {"type": "agent_toolset_20260401"},
            *memory_tool_definitions(),
        ],
    )

Usage — event loop (manual):
    with client.beta.sessions.events.stream(session.id) as stream:
        events_by_id = {}
        for event in stream:
            if event.type == "agent.custom_tool_use":
                events_by_id[event.id] = event

            elif event.type == "session.status_idle" and (stop := event.stop_reason):
                if stop.type == "requires_action":
                    for event_id in stop.event_ids:
                        tool_event = events_by_id[event_id]
                        if is_memory_tool(tool_event.name):
                            result = await handle_memory_tool(
                                brain, tool_event.name, tool_event.input,
                                bank_id="user-123",
                            )
                            client.beta.sessions.events.send(
                                session.id,
                                events=[{
                                    "type": "user.custom_tool_result",
                                    "custom_tool_use_id": event_id,
                                    "content": [{"type": "text", "text": result}],
                                }],
                            )
                elif stop.type == "end_turn":
                    break

Usage — high-level helper:
    result = await run_session_with_memory(
        client, brain,
        session_id=session.id,
        prompt="What do you remember about my preferences?",
        bank_id="user-123",
    )
    print(result)

    # When deleting the session, send user.interrupt first (see delete_managed_session).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte

from astrocyte.types import AstrocyteContext


# ---------------------------------------------------------------------------
# Tool names — canonical set
# ---------------------------------------------------------------------------

MEMORY_RETAIN = "memory_retain"
MEMORY_RECALL = "memory_recall"
MEMORY_REFLECT = "memory_reflect"
MEMORY_FORGET = "memory_forget"

_ALL_MEMORY_TOOLS = {MEMORY_RETAIN, MEMORY_RECALL, MEMORY_REFLECT, MEMORY_FORGET}


def is_memory_tool(name: str) -> bool:
    """Check whether a tool name is an Astrocyte memory tool."""
    return name in _ALL_MEMORY_TOOLS


# ---------------------------------------------------------------------------
# Tool definitions for agent creation
# ---------------------------------------------------------------------------


def memory_tool_definitions(
    *,
    include_reflect: bool = True,
    include_forget: bool = False,
) -> list[dict[str, Any]]:
    """Return custom tool definitions for ``client.beta.agents.create(tools=...)``.

    Each dict has ``type: "custom"`` plus ``name``, ``description``, and
    ``input_schema`` — the format expected by the Managed Agents API.

    Args:
        include_reflect: Include the ``memory_reflect`` tool (default True).
        include_forget: Include the ``memory_forget`` tool (default False).

    Returns:
        List of tool definition dicts ready to spread into the agent's tool list.
    """
    tools: list[dict[str, Any]] = []

    tools.append({
        "type": "custom",
        "name": MEMORY_RETAIN,
        "description": (
            "Store content into long-term memory for future recall. "
            "Use this to remember facts, preferences, decisions, or any information "
            "that should persist across conversations. "
            "Optionally pass comma-separated tags for categorization."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The text content to store in memory.",
                },
                "tags": {
                    "type": "string",
                    "description": "Optional comma-separated tags (e.g. 'preference,important').",
                },
            },
            "required": ["content"],
        },
    })

    tools.append({
        "type": "custom",
        "name": MEMORY_RECALL,
        "description": (
            "Search long-term memory for information relevant to a query. "
            "Returns scored hits ranked by relevance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to find relevant memories.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5).",
                },
            },
            "required": ["query"],
        },
    })

    if include_reflect:
        tools.append({
            "type": "custom",
            "name": MEMORY_REFLECT,
            "description": (
                "Synthesize a comprehensive answer from long-term memory. "
                "Use this instead of recall when you need a narrative answer "
                "rather than raw search hits."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The question to answer from memory.",
                    },
                },
                "required": ["query"],
            },
        })

    if include_forget:
        tools.append({
            "type": "custom",
            "name": MEMORY_FORGET,
            "description": (
                "Remove specific memories by their IDs. "
                "Pass a comma-separated list of memory IDs to delete."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "memory_ids": {
                        "type": "string",
                        "description": "Comma-separated memory IDs to delete.",
                    },
                },
                "required": ["memory_ids"],
            },
        })

    return tools


# ---------------------------------------------------------------------------
# Tag parsing helper
# ---------------------------------------------------------------------------


def _parse_tags(raw: Any) -> list[str] | None:
    """Parse comma-separated tag string into a list."""
    if isinstance(raw, str) and raw.strip():
        return [t.strip() for t in raw.split(",") if t.strip()]
    return None


# ---------------------------------------------------------------------------
# Tool handler — called from your event loop
# ---------------------------------------------------------------------------


async def handle_memory_tool(
    brain: Astrocyte,
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    bank_id: str,
    context: AstrocyteContext | None = None,
) -> str:
    """Execute an Astrocyte memory tool and return the result as a string.

    Call this from your SSE event loop when you receive an
    ``agent.custom_tool_use`` event for a memory tool.

    Args:
        brain: The Astrocyte instance.
        tool_name: The tool name from the event (e.g. ``"memory_retain"``).
        tool_input: The tool input dict from the event.
        bank_id: The memory bank to operate on.

    Returns:
        A JSON string (or plain text for reflect) to send back as
        ``user.custom_tool_result`` content.

    Raises:
        ValueError: If ``tool_name`` is not a recognized memory tool.
    """
    if tool_name == MEMORY_RETAIN:
        tag_list = _parse_tags(tool_input.get("tags"))
        result = await brain.retain(
            tool_input["content"], bank_id=bank_id, tags=tag_list, context=context
        )
        return json.dumps({
            "stored": result.stored,
            "memory_id": result.memory_id,
        })

    elif tool_name == MEMORY_RECALL:
        max_results = tool_input.get("max_results", 5)
        result = await brain.recall(
            tool_input["query"], bank_id=bank_id, max_results=max_results, context=context
        )
        hits = [
            {"text": h.text, "score": round(h.score, 4)}
            for h in result.hits
        ]
        return json.dumps({"hits": hits, "total": result.total_available})

    elif tool_name == MEMORY_REFLECT:
        result = await brain.reflect(tool_input["query"], bank_id=bank_id, context=context)
        return result.answer

    elif tool_name == MEMORY_FORGET:
        ids_raw = tool_input["memory_ids"]
        memory_ids = [mid.strip() for mid in ids_raw.split(",") if mid.strip()]
        result = await brain.forget(bank_id, memory_ids=memory_ids, context=context)
        return json.dumps({"deleted_count": result.deleted_count})

    else:
        raise ValueError(f"Unknown memory tool: {tool_name}")


# ---------------------------------------------------------------------------
# High-level session runner
# ---------------------------------------------------------------------------


def _format_managed_session_error(event: Any) -> str:
    """Best-effort message for ``session.error`` events (SDK shapes vary)."""
    err = getattr(event, "error", None)
    if err is None:
        return repr(event)
    if isinstance(err, str):
        return err
    model_dump = getattr(err, "model_dump", None)
    if callable(model_dump):
        payload = None
        try:
            payload = model_dump(mode="json")
        except Exception:
            try:
                payload = model_dump()
            except Exception:
                pass
        if payload is not None:
            try:
                return json.dumps(payload, default=str)
            except Exception:
                pass
    parts: list[str] = []
    for key in ("type", "message", "code", "detail"):
        val = getattr(err, key, None)
        if val is not None:
            parts.append(f"{key}={val!r}")
    return ", ".join(parts) if parts else repr(err)


async def run_session_with_memory(
    client: Any,
    brain: Astrocyte,
    *,
    session_id: str,
    prompt: str,
    bank_id: str,
    context: AstrocyteContext | None = None,
    non_memory_tool_handler: Any | None = None,
    timeout_seconds: float = 120,
) -> str:
    """Run a Managed Agents session, handling Astrocyte memory tool calls.

    Opens an SSE stream, sends the prompt, and processes events until
    the agent finishes (``end_turn``). Memory tool calls are handled
    automatically; non-memory custom tool calls are delegated to
    ``non_memory_tool_handler`` if provided.

    Args:
        client: An ``anthropic.Anthropic`` client instance.
        brain: The Astrocyte instance.
        session_id: The session ID to interact with.
        prompt: The user message to send.
        bank_id: The memory bank for all memory operations.
        non_memory_tool_handler: Optional async callable
            ``(name: str, input: dict) -> str`` for non-memory custom tools.
        timeout_seconds: Maximum time to wait for completion.

    Returns:
        The concatenated agent message text from the session.
    """
    import asyncio

    agent_text_parts: list[str] = []
    events_by_id: dict[str, Any] = {}

    async def _run() -> str:
        with client.beta.sessions.events.stream(session_id) as stream:
            # Send the user message after the stream opens
            client.beta.sessions.events.send(
                session_id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": prompt}],
                    },
                ],
            )

            for event in stream:
                if event.type == "agent.message":
                    for block in event.content:
                        if hasattr(block, "text"):
                            agent_text_parts.append(block.text)

                elif event.type == "agent.custom_tool_use":
                    events_by_id[event.id] = event

                elif event.type == "session.error":
                    raise RuntimeError(
                        "Managed Agents session error: "
                        f"{_format_managed_session_error(event)}"
                    )

                elif event.type == "session.status_idle":
                    stop = getattr(event, "stop_reason", None)
                    if stop is None:
                        continue

                    if stop.type == "requires_action":
                        for event_id in stop.event_ids:
                            tool_event = events_by_id.get(event_id)
                            if tool_event is None:
                                continue

                            if is_memory_tool(tool_event.name):
                                result_text = await handle_memory_tool(
                                    brain,
                                    tool_event.name,
                                    tool_event.input,
                                    bank_id=bank_id,
                                    context=context,
                                )
                            elif non_memory_tool_handler is not None:
                                result_text = await non_memory_tool_handler(
                                    tool_event.name, tool_event.input
                                )
                            else:
                                result_text = json.dumps({
                                    "error": f"No handler for tool: {tool_event.name}"
                                })

                            client.beta.sessions.events.send(
                                session_id,
                                events=[
                                    {
                                        "type": "user.custom_tool_result",
                                        "custom_tool_use_id": event_id,
                                        "content": [
                                            {"type": "text", "text": result_text}
                                        ],
                                    },
                                ],
                            )

                    elif stop.type == "end_turn":
                        break

                elif event.type == "session.status_terminated":
                    error_msg = getattr(event, "error", None)
                    raise RuntimeError(
                        f"Session terminated: {error_msg}"
                    )

        return "".join(agent_text_parts)

    return await asyncio.wait_for(_run(), timeout=timeout_seconds)


def delete_managed_session(client: Any, session_id: str) -> None:
    """Delete a Managed Agents session.

    The API returns 400 if the session is still considered **running** (for
    example immediately after closing the per-turn SSE stream). In that case
    the error says to send a ``user.interrupt`` event or wait for completion.
    We send ``user.interrupt`` first (errors ignored if already idle), then
    ``DELETE`` the session.
    """
    try:
        client.beta.sessions.events.send(
            session_id,
            events=[{"type": "user.interrupt"}],
        )
    except Exception:
        pass
    client.beta.sessions.delete(session_id)
