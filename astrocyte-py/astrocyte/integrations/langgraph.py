"""LangGraph / LangChain integration — Astrocyte as a memory store.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.langgraph import AstrocyteMemory

    brain = Astrocyte.from_config("astrocyte.yaml")
    memory = AstrocyteMemory(brain, bank_id="user-123")

    # Use as LangGraph memory store
    graph = StateGraph(AgentState)
    app = graph.compile(checkpointer=memory)

Maps:
    - put / save_context → brain.retain()
    - search → brain.recall()
    - Thread ID → bank ID (configurable via thread_to_bank)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


class AstrocyteMemory:
    """Astrocyte-backed memory for LangGraph / LangChain agents.

    Implements the interface pattern expected by LangGraph's memory store:
    save_context(), load_memory_variables(), search().

    This is a thin wrapper — all policy enforcement happens inside Astrocyte.
    """

    def __init__(
        self,
        brain: Astrocyte,
        bank_id: str,
        *,
        auto_retain: bool = False,
        auto_retain_filter: str | None = None,
        thread_to_bank: dict[str, str] | None = None,
    ) -> None:
        self.brain = brain
        self.bank_id = bank_id
        self.auto_retain = auto_retain
        self.auto_retain_filter = auto_retain_filter
        self._thread_to_bank = thread_to_bank or {}

    def _resolve_bank(self, thread_id: str | None = None) -> str:
        """Map thread ID to bank ID, falling back to default."""
        if thread_id and thread_id in self._thread_to_bank:
            return self._thread_to_bank[thread_id]
        return self.bank_id

    async def save_context(
        self,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        *,
        thread_id: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Save interaction context to memory (retain).

        Combines inputs and outputs into a single memory entry.
        """
        bank = self._resolve_bank(thread_id)

        # Build a concise representation of the interaction
        parts: list[str] = []
        if inputs:
            for key, value in inputs.items():
                parts.append(f"{key}: {value}")
        if outputs:
            for key, value in outputs.items():
                parts.append(f"{key}: {value}")

        if not parts:
            return

        content = "\n".join(parts)
        await self.brain.retain(
            content,
            bank_id=bank,
            tags=tags or ["langgraph"],
            metadata={"source": "langgraph", "thread_id": thread_id or ""},
        )

    async def search(
        self,
        query: str,
        *,
        thread_id: str | None = None,
        max_results: int = 5,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search memory (recall).

        Returns a list of dicts with 'text', 'score', and 'metadata'.
        """
        bank = self._resolve_bank(thread_id)
        result = await self.brain.recall(
            query,
            bank_id=bank,
            max_results=max_results,
            tags=tags,
        )
        return [
            {
                "text": hit.text,
                "score": hit.score,
                "metadata": hit.metadata,
                "memory_id": hit.memory_id,
            }
            for hit in result.hits
        ]

    async def load_memory_variables(
        self,
        inputs: dict[str, Any],
        *,
        thread_id: str | None = None,
    ) -> dict[str, str]:
        """Load relevant memories for the current input (LangChain pattern).

        Returns {"memory": "formatted memories"} for injection into prompts.
        """
        query = " ".join(str(v) for v in inputs.values())
        if not query.strip():
            return {"memory": ""}

        hits = await self.search(query, thread_id=thread_id)
        if not hits:
            return {"memory": ""}

        formatted = "\n".join(f"- {h['text']}" for h in hits)
        return {"memory": formatted}

    # Sync wrappers for frameworks that don't support async
    def save_context_sync(self, inputs: dict, outputs: dict, **kwargs: Any) -> None:
        """Synchronous wrapper for save_context."""
        asyncio.get_event_loop().run_until_complete(self.save_context(inputs, outputs, **kwargs))

    def search_sync(self, query: str, **kwargs: Any) -> list[dict]:
        """Synchronous wrapper for search."""
        return asyncio.get_event_loop().run_until_complete(self.search(query, **kwargs))
