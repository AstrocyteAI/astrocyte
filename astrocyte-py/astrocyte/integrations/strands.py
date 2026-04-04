"""Strands Agents (AWS) integration — Astrocyte as agent tools.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.strands import astrocyte_strands_tools

    brain = Astrocyte.from_config("astrocyte.yaml")
    tools = astrocyte_strands_tools(brain, bank_id="user-123")

    # Register with Strands agent
    from strands import Agent
    agent = Agent(model=model, tools=tools)

Strands Agents uses a tool specification pattern where each tool has:
- A spec dict (name, description, inputSchema in JSON Schema format)
- A handler function that receives the tool input
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


def astrocyte_strands_tools(
    brain: Astrocyte,
    bank_id: str,
    *,
    include_reflect: bool = True,
    include_forget: bool = False,
) -> list[dict[str, Any]]:
    """Create Strands-compatible tool definitions backed by Astrocyte.

    Returns a list of dicts, each with 'spec' (JSON Schema) and 'handler' (async callable).
    Strands Agents registers tools with this pattern.
    """
    tools: list[dict[str, Any]] = []

    # ── memory_retain ──

    async def retain_handler(tool_input: dict[str, Any]) -> str:
        content = tool_input["content"]
        tags = tool_input.get("tags")
        result = await brain.retain(content, bank_id=bank_id, tags=tags)
        return json.dumps({"stored": result.stored, "memory_id": result.memory_id})

    tools.append(
        {
            "spec": {
                "name": "memory_retain",
                "description": "Store content into long-term memory for future recall.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "The text to memorize."},
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional tags for filtering.",
                            },
                        },
                        "required": ["content"],
                    }
                },
            },
            "handler": retain_handler,
        }
    )

    # ── memory_recall ──

    async def recall_handler(tool_input: dict[str, Any]) -> str:
        query = tool_input["query"]
        max_results = tool_input.get("max_results", 5)
        result = await brain.recall(query, bank_id=bank_id, max_results=max_results)
        hits = [{"text": h.text, "score": round(h.score, 4)} for h in result.hits]
        return json.dumps({"hits": hits, "total": result.total_available})

    tools.append(
        {
            "spec": {
                "name": "memory_recall",
                "description": "Search long-term memory for information relevant to a query.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Natural language search query."},
                            "max_results": {"type": "integer", "description": "Maximum results.", "default": 5},
                        },
                        "required": ["query"],
                    }
                },
            },
            "handler": recall_handler,
        }
    )

    # ── memory_reflect ──

    if include_reflect:

        async def reflect_handler(tool_input: dict[str, Any]) -> str:
            query = tool_input["query"]
            result = await brain.reflect(query, bank_id=bank_id)
            return json.dumps({"answer": result.answer})

        tools.append(
            {
                "spec": {
                    "name": "memory_reflect",
                    "description": "Synthesize a comprehensive answer from long-term memory.",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "The question to answer."},
                            },
                            "required": ["query"],
                        }
                    },
                },
                "handler": reflect_handler,
            }
        )

    # ── memory_forget ──

    if include_forget:

        async def forget_handler(tool_input: dict[str, Any]) -> str:
            memory_ids = tool_input["memory_ids"]
            result = await brain.forget(bank_id, memory_ids=memory_ids)
            return json.dumps({"deleted_count": result.deleted_count})

        tools.append(
            {
                "spec": {
                    "name": "memory_forget",
                    "description": "Remove specific memories by their IDs.",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "memory_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "IDs of memories to delete.",
                                },
                            },
                            "required": ["memory_ids"],
                        }
                    },
                },
                "handler": forget_handler,
            }
        )

    return tools
