"""OpenAI-compatible tool definitions for Astrocyte memory.

Works with OpenAI Agents SDK, Claude Agent SDK, or any system
that accepts OpenAI-format function/tool definitions.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.openai_agents import astrocyte_tool_definitions

    brain = Astrocyte.from_config("astrocyte.yaml")
    tools, handlers = astrocyte_tool_definitions(brain, bank_id="user-123")

    # tools: list of OpenAI-format tool dicts (for the API)
    # handlers: dict[name → async callable] (for dispatching tool calls)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte

from astrocyte.types import AstrocyteContext

# Type alias for handler functions
ToolHandler = Callable[..., Awaitable[str]]


def astrocyte_tool_definitions(
    brain: Astrocyte,
    bank_id: str,
    *,
    context: AstrocyteContext | None = None,
    include_reflect: bool = True,
    include_forget: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, ToolHandler]]:
    """Create OpenAI-format tool definitions backed by Astrocyte.

    Returns:
        (tools, handlers) where:
        - tools: list of dicts in OpenAI function-calling format
        - handlers: dict mapping tool name → async handler function

    The tools list can be passed directly to OpenAI's `tools` parameter.
    When the model calls a tool, look up the handler by name and call it.
    """
    tools: list[dict[str, Any]] = []
    handlers: dict[str, ToolHandler] = {}

    # ── memory_retain ──

    tools.append(
        {
            "type": "function",
            "function": {
                "name": "memory_retain",
                "description": "Store content into long-term memory for future recall.",
                "parameters": {
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
                },
            },
        }
    )

    async def _retain(content: str, tags: list[str] | None = None, **kwargs: Any) -> str:
        result = await brain.retain(content, bank_id=bank_id, tags=tags, context=context)
        return json.dumps({"stored": result.stored, "memory_id": result.memory_id, "error": result.error})

    handlers["memory_retain"] = _retain

    # ── memory_recall ──

    tools.append(
        {
            "type": "function",
            "function": {
                "name": "memory_recall",
                "description": "Search long-term memory for information relevant to a query.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural language search query."},
                        "max_results": {"type": "integer", "default": 5, "description": "Maximum results."},
                    },
                    "required": ["query"],
                },
            },
        }
    )

    async def _recall(query: str, max_results: int = 5, **kwargs: Any) -> str:
        result = await brain.recall(query, bank_id=bank_id, max_results=max_results, context=context)
        hits = [{"text": h.text, "score": round(h.score, 4)} for h in result.hits]
        return json.dumps({"hits": hits, "total": result.total_available})

    handlers["memory_recall"] = _recall

    # ── memory_reflect ──

    if include_reflect:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "memory_reflect",
                    "description": "Synthesize a comprehensive answer from long-term memory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "The question to answer from memory."},
                        },
                        "required": ["query"],
                    },
                },
            }
        )

        async def _reflect(query: str, **kwargs: Any) -> str:
            result = await brain.reflect(query, bank_id=bank_id, context=context)
            return json.dumps({"answer": result.answer})

        handlers["memory_reflect"] = _reflect

    # ── memory_forget ──

    if include_forget:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "memory_forget",
                    "description": "Remove specific memories by their IDs.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "memory_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "IDs of memories to delete.",
                            },
                        },
                        "required": ["memory_ids"],
                    },
                },
            }
        )

        async def _forget(memory_ids: list[str], **kwargs: Any) -> str:
            result = await brain.forget(bank_id, memory_ids=memory_ids, context=context)
            return json.dumps({"deleted_count": result.deleted_count})

        handlers["memory_forget"] = _forget

    return tools, handlers
