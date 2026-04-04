"""Pydantic AI integration — Astrocytes as agent tools.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.pydantic_ai import astrocyte_tools

    brain = Astrocyte.from_config("astrocyte.yaml")
    tools = astrocyte_tools(brain, bank_id="user-123")

    agent = Agent(model="claude-sonnet-4-20250514", tools=tools)

Exposes retain, recall, reflect as tool functions that
Pydantic AI agents can call during execution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


def astrocyte_tools(
    brain: Astrocyte,
    bank_id: str,
    *,
    include_reflect: bool = True,
    include_forget: bool = False,
) -> list[dict[str, Any]]:
    """Create Pydantic AI-compatible tool definitions backed by Astrocytes.

    Returns a list of tool definition dicts that Pydantic AI can register.
    Each tool is a dict with 'name', 'description', 'function', and 'parameters'.
    """
    tools: list[dict[str, Any]] = []

    async def memory_retain(content: str, tags: list[str] | None = None) -> str:
        """Store content into long-term memory."""
        result = await brain.retain(content, bank_id=bank_id, tags=tags)
        if result.stored:
            return f"Stored memory (id: {result.memory_id})"
        return f"Failed to store: {result.error}"

    tools.append(
        {
            "name": "memory_retain",
            "description": "Store content into long-term memory for future recall.",
            "function": memory_retain,
        }
    )

    async def memory_recall(query: str, max_results: int = 5) -> str:
        """Search long-term memory for relevant information."""
        result = await brain.recall(query, bank_id=bank_id, max_results=max_results)
        if not result.hits:
            return "No relevant memories found."
        lines = [f"- [{h.score:.2f}] {h.text}" for h in result.hits]
        return f"Found {len(result.hits)} memories:\n" + "\n".join(lines)

    tools.append(
        {
            "name": "memory_recall",
            "description": "Search long-term memory for information relevant to a query.",
            "function": memory_recall,
        }
    )

    if include_reflect:

        async def memory_reflect(query: str) -> str:
            """Synthesize an answer from long-term memory."""
            result = await brain.reflect(query, bank_id=bank_id)
            return result.answer

        tools.append(
            {
                "name": "memory_reflect",
                "description": "Synthesize a comprehensive answer from long-term memory.",
                "function": memory_reflect,
            }
        )

    if include_forget:

        async def memory_forget(memory_ids: list[str]) -> str:
            """Remove specific memories by their IDs."""
            result = await brain.forget(bank_id, memory_ids=memory_ids)
            return f"Deleted {result.deleted_count} memories."

        tools.append(
            {
                "name": "memory_forget",
                "description": "Remove specific memories by their IDs.",
                "function": memory_forget,
            }
        )

    return tools
