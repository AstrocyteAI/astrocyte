"""LlamaIndex integration — Astrocyte as a memory store for LlamaIndex agents.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.llamaindex import AstrocyteLlamaMemory

    brain = Astrocyte.from_config("astrocyte.yaml")
    memory = AstrocyteLlamaMemory(brain, bank_id="user-123")

    # Use with LlamaIndex chat engine
    chat_engine = index.as_chat_engine(memory=memory)

    # Or standalone
    await memory.put("Calvin prefers dark mode")
    results = await memory.get("What does Calvin prefer?")

Maps:
    - put → brain.retain()
    - get / search → brain.recall()
    - LlamaIndex expects memory.get() to return formatted string context
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


class AstrocyteLlamaMemory:
    """Astrocyte-backed memory for LlamaIndex agents and chat engines.

    Implements the interface pattern expected by LlamaIndex's memory system:
    put(), get(), get_all(), reset().
    """

    def __init__(
        self,
        brain: Astrocyte,
        bank_id: str,
        *,
        max_results: int = 10,
    ) -> None:
        self.brain = brain
        self.bank_id = bank_id
        self.max_results = max_results

    async def put(
        self,
        content: str,
        *,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Store content into memory. Returns memory_id."""
        result = await self.brain.retain(
            content,
            bank_id=self.bank_id,
            tags=tags or ["llamaindex"],
            metadata=metadata or {"source": "llamaindex"},
        )
        return result.memory_id if result.stored else None

    async def get(
        self,
        query: str,
        *,
        max_results: int | None = None,
    ) -> str:
        """Retrieve relevant memories as formatted text (for prompt injection).

        Returns a string of formatted memory hits, suitable for
        injection into system or user messages.
        """
        result = await self.brain.recall(
            query,
            bank_id=self.bank_id,
            max_results=max_results or self.max_results,
        )
        if not result.hits:
            return ""
        return "\n".join(f"- {h.text}" for h in result.hits)

    async def get_all(self) -> list[dict[str, Any]]:
        """Retrieve all memories in the bank (for full context).

        Returns list of dicts with text, score, metadata.
        """
        result = await self.brain.recall(
            "*",
            bank_id=self.bank_id,
            max_results=1000,
        )
        return [
            {
                "text": h.text,
                "score": h.score,
                "metadata": h.metadata,
                "memory_id": h.memory_id,
            }
            for h in result.hits
        ]

    async def search(
        self,
        query: str,
        *,
        max_results: int | None = None,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search memory with structured results (for programmatic access)."""
        result = await self.brain.recall(
            query,
            bank_id=self.bank_id,
            max_results=max_results or self.max_results,
            tags=tags,
        )
        return [
            {
                "text": h.text,
                "score": h.score,
                "metadata": h.metadata,
                "memory_id": h.memory_id,
            }
            for h in result.hits
        ]

    async def reset(self) -> None:
        """Clear all memories in the bank."""
        await self.brain.clear_bank(self.bank_id)
