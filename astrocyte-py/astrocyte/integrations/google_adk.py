"""Google ADK (Agent Development Kit) integration.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.google_adk import astrocyte_adk_tools

    brain = Astrocyte.from_config("astrocyte.yaml")
    tools = astrocyte_adk_tools(brain, bank_id="user-123")

    # Register with ADK agent
    agent = Agent(model="gemini-2.0-flash", tools=tools)

Google ADK uses a callable-based tool pattern. Each tool is an async function
with a docstring (used as the tool description) and type-annotated parameters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


def astrocyte_adk_tools(
    brain: Astrocyte,
    bank_id: str,
    *,
    include_reflect: bool = True,
    include_forget: bool = False,
) -> list:
    """Create Google ADK-compatible tool functions backed by Astrocyte.

    Returns a list of async callables that ADK can register as tools.
    Each function has proper type annotations and docstrings for ADK schema generation.
    """
    tools: list = []

    async def memory_retain(content: str, tags: str | None = None) -> dict[str, Any]:
        """Store content into long-term memory for future recall.

        Args:
            content: The text to memorize.
            tags: Comma-separated tags for filtering (optional).
        """
        tag_list = [t.strip() for t in tags.split(",")] if tags else None
        result = await brain.retain(content, bank_id=bank_id, tags=tag_list)
        return {"stored": result.stored, "memory_id": result.memory_id}

    tools.append(memory_retain)

    async def memory_recall(query: str, max_results: int = 5) -> dict[str, Any]:
        """Search long-term memory for information relevant to a query.

        Args:
            query: Natural language search query.
            max_results: Maximum number of results to return.
        """
        result = await brain.recall(query, bank_id=bank_id, max_results=max_results)
        return {
            "hits": [{"text": h.text, "score": round(h.score, 4)} for h in result.hits],
            "total": result.total_available,
        }

    tools.append(memory_recall)

    if include_reflect:

        async def memory_reflect(query: str) -> dict[str, str]:
            """Synthesize a comprehensive answer from long-term memory.

            Args:
                query: The question to answer from memory.
            """
            result = await brain.reflect(query, bank_id=bank_id)
            return {"answer": result.answer}

        tools.append(memory_reflect)

    if include_forget:

        async def memory_forget(memory_ids: str) -> dict[str, int]:
            """Remove specific memories by their IDs.

            Args:
                memory_ids: Comma-separated memory IDs to delete.
            """
            ids = [mid.strip() for mid in memory_ids.split(",")]
            result = await brain.forget(bank_id, memory_ids=ids)
            return {"deleted_count": result.deleted_count}

        tools.append(memory_forget)

    return tools
