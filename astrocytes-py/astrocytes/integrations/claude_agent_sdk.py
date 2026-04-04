"""Claude Agent SDK integration — Astrocytes memory as native SDK tools.

Usage:
    from astrocytes import Astrocyte
    from astrocytes.integrations.claude_agent_sdk import astrocyte_claude_agent_server

    brain = Astrocyte.from_config("astrocytes.yaml")
    memory_server = astrocyte_claude_agent_server(brain, bank_id="user-123")

    # Use with Claude Agent SDK
    from claude_agent_sdk import query, ClaudeAgentOptions

    options = ClaudeAgentOptions(
        mcp_servers={"memory": memory_server},
        allowed_tools=["mcp__memory__*"],
    )

    async for message in query(prompt="What do you remember?", options=options):
        ...

The Claude Agent SDK uses @tool + create_sdk_mcp_server. Tools receive
args as dict[str, Any] and return {"content": [{"type": "text", "text": ...}]}.
Tools are registered as in-process MCP servers — no separate process needed.

This integration provides TWO options:
1. astrocyte_claude_agent_server() — returns an SDK MCP server (requires claude_agent_sdk installed)
2. astrocyte_claude_agent_tools() — returns tool definition dicts (no SDK dependency, for testing)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocytes._astrocyte import Astrocyte


# ---------------------------------------------------------------------------
# Tool definitions as plain dicts (no SDK dependency)
# ---------------------------------------------------------------------------


def astrocyte_claude_agent_tools(
    brain: Astrocyte,
    bank_id: str,
    *,
    include_reflect: bool = True,
    include_forget: bool = False,
) -> list[dict[str, Any]]:
    """Create Claude Agent SDK tool definitions as plain dicts.

    Each dict has: name, description, input_schema, handler.
    The handler follows the SDK pattern: receives dict[str, Any], returns
    {"content": [{"type": "text", "text": "..."}]}.

    This works WITHOUT claude_agent_sdk installed (for testing).
    To get an actual SDK MCP server, use astrocyte_claude_agent_server().
    """
    tools: list[dict[str, Any]] = []

    # ── memory_retain ──

    async def retain_handler(args: dict[str, Any]) -> dict[str, Any]:
        content = args["content"]
        tags = args.get("tags")
        tag_list = [t.strip() for t in tags.split(",")] if isinstance(tags, str) else tags
        result = await brain.retain(content, bank_id=bank_id, tags=tag_list)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"stored": result.stored, "memory_id": result.memory_id}),
                }
            ]
        }

    tools.append(
        {
            "name": "memory_retain",
            "description": "Store content into long-term memory for future recall.",
            "input_schema": {"content": str},
            "handler": retain_handler,
        }
    )

    # ── memory_recall ──

    async def recall_handler(args: dict[str, Any]) -> dict[str, Any]:
        query = args["query"]
        max_results = args.get("max_results", 5)
        result = await brain.recall(query, bank_id=bank_id, max_results=max_results)
        hits = [{"text": h.text, "score": round(h.score, 4)} for h in result.hits]
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"hits": hits, "total": result.total_available}),
                }
            ]
        }

    tools.append(
        {
            "name": "memory_recall",
            "description": "Search long-term memory for information relevant to a query.",
            "input_schema": {"query": str, "max_results": int},
            "handler": recall_handler,
        }
    )

    # ── memory_reflect ──

    if include_reflect:

        async def reflect_handler(args: dict[str, Any]) -> dict[str, Any]:
            query = args["query"]
            result = await brain.reflect(query, bank_id=bank_id)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": result.answer,
                    }
                ]
            }

        tools.append(
            {
                "name": "memory_reflect",
                "description": "Synthesize a comprehensive answer from long-term memory.",
                "input_schema": {"query": str},
                "handler": reflect_handler,
            }
        )

    # ── memory_forget ──

    if include_forget:

        async def forget_handler(args: dict[str, Any]) -> dict[str, Any]:
            memory_ids = args["memory_ids"]
            if isinstance(memory_ids, str):
                memory_ids = [mid.strip() for mid in memory_ids.split(",")]
            result = await brain.forget(bank_id, memory_ids=memory_ids)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"deleted_count": result.deleted_count}),
                    }
                ]
            }

        tools.append(
            {
                "name": "memory_forget",
                "description": "Remove specific memories by their IDs.",
                "input_schema": {"memory_ids": str},
                "handler": forget_handler,
            }
        )

    return tools


# ---------------------------------------------------------------------------
# SDK MCP server (requires claude_agent_sdk installed)
# ---------------------------------------------------------------------------


def astrocyte_claude_agent_server(
    brain: Astrocyte,
    bank_id: str,
    *,
    server_name: str = "astrocytes_memory",
    include_reflect: bool = True,
    include_forget: bool = False,
) -> Any:
    """Create a Claude Agent SDK in-process MCP server backed by Astrocytes.

    Requires claude_agent_sdk to be installed.
    Returns an MCP server that can be passed to ClaudeAgentOptions.mcp_servers.

    Usage:
        memory_server = astrocyte_claude_agent_server(brain, bank_id="user-123")
        options = ClaudeAgentOptions(
            mcp_servers={"memory": memory_server},
            allowed_tools=["mcp__memory__*"],
        )
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    sdk_tools = []

    @tool(
        "memory_retain",
        "Store content into long-term memory for future recall.",
        {"content": str},
    )
    async def memory_retain(args: dict[str, Any]) -> dict[str, Any]:
        content = args["content"]
        tags = args.get("tags")
        tag_list = [t.strip() for t in tags.split(",")] if isinstance(tags, str) else tags
        result = await brain.retain(content, bank_id=bank_id, tags=tag_list)
        return {
            "content": [{"type": "text", "text": json.dumps({"stored": result.stored, "memory_id": result.memory_id})}]
        }

    sdk_tools.append(memory_retain)

    @tool(
        "memory_recall",
        "Search long-term memory for information relevant to a query.",
        {"query": str, "max_results": int},
    )
    async def memory_recall(args: dict[str, Any]) -> dict[str, Any]:
        query_text = args["query"]
        max_results = args.get("max_results", 5)
        result = await brain.recall(query_text, bank_id=bank_id, max_results=max_results)
        hits = [{"text": h.text, "score": round(h.score, 4)} for h in result.hits]
        return {"content": [{"type": "text", "text": json.dumps({"hits": hits, "total": result.total_available})}]}

    sdk_tools.append(memory_recall)

    if include_reflect:

        @tool(
            "memory_reflect",
            "Synthesize a comprehensive answer from long-term memory.",
            {"query": str},
        )
        async def memory_reflect(args: dict[str, Any]) -> dict[str, Any]:
            result = await brain.reflect(args["query"], bank_id=bank_id)
            return {"content": [{"type": "text", "text": result.answer}]}

        sdk_tools.append(memory_reflect)

    if include_forget:

        @tool(
            "memory_forget",
            "Remove specific memories by their IDs (comma-separated).",
            {"memory_ids": str},
        )
        async def memory_forget(args: dict[str, Any]) -> dict[str, Any]:
            ids = [mid.strip() for mid in args["memory_ids"].split(",")]
            result = await brain.forget(bank_id, memory_ids=ids)
            return {"content": [{"type": "text", "text": json.dumps({"deleted_count": result.deleted_count})}]}

        sdk_tools.append(memory_forget)

    return create_sdk_mcp_server(
        name=server_name,
        version="1.0.0",
        tools=sdk_tools,
    )
