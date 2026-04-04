"""Microsoft Semantic Kernel integration — Astrocytes as kernel plugins.

Usage:
    from astrocytes import Astrocyte
    from astrocytes.integrations.semantic_kernel import AstrocytePlugin

    brain = Astrocyte.from_config("astrocytes.yaml")
    plugin = AstrocytePlugin(brain, bank_id="user-123")

    # Register with Semantic Kernel
    kernel = Kernel()
    kernel.add_plugin(plugin, plugin_name="memory")

Semantic Kernel uses a plugin/function pattern. Each plugin method decorated
with @kernel_function becomes a callable function. We emulate this pattern
with plain methods that follow the same signature conventions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocytes._astrocyte import Astrocyte


class AstrocytePlugin:
    """Astrocytes memory plugin for Microsoft Semantic Kernel.

    Methods follow the Semantic Kernel @kernel_function pattern:
    - Named methods with docstrings (used as function descriptions)
    - Type-annotated parameters (used for schema generation)
    - Return strings (Semantic Kernel standard for text results)

    Register with: kernel.add_plugin(plugin, plugin_name="memory")
    """

    def __init__(
        self,
        brain: Astrocyte,
        bank_id: str,
        *,
        include_reflect: bool = True,
        include_forget: bool = False,
    ) -> None:
        self.brain = brain
        self.bank_id = bank_id
        self._include_reflect = include_reflect
        self._include_forget = include_forget

    async def retain(self, content: str, tags: str = "") -> str:
        """Store content into long-term memory for future recall.

        Args:
            content: The text to memorize.
            tags: Comma-separated tags for filtering (optional).
        """
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        result = await self.brain.retain(content, bank_id=self.bank_id, tags=tag_list)
        if result.stored:
            return f"Stored memory (id: {result.memory_id})"
        return f"Failed to store: {result.error}"

    async def recall(self, query: str, max_results: int = 5) -> str:
        """Search long-term memory for information relevant to a query.

        Args:
            query: Natural language search query.
            max_results: Maximum number of results to return.
        """
        result = await self.brain.recall(query, bank_id=self.bank_id, max_results=max_results)
        if not result.hits:
            return "No relevant memories found."
        lines = [f"- [{h.score:.2f}] {h.text}" for h in result.hits]
        return f"Found {len(result.hits)} memories:\n" + "\n".join(lines)

    async def reflect(self, query: str) -> str:
        """Synthesize a comprehensive answer from long-term memory.

        Args:
            query: The question to answer from memory.
        """
        result = await self.brain.reflect(query, bank_id=self.bank_id)
        return result.answer

    async def forget(self, memory_ids: str) -> str:
        """Remove specific memories by their IDs.

        Args:
            memory_ids: Comma-separated memory IDs to delete.
        """
        ids = [mid.strip() for mid in memory_ids.split(",")]
        result = await self.brain.forget(self.bank_id, memory_ids=ids)
        return f"Deleted {result.deleted_count} memories."

    def get_functions(self) -> list[dict[str, Any]]:
        """Return the list of available functions for registration.

        Each dict has 'name', 'description', 'function' for Semantic Kernel.
        """
        fns: list[dict[str, Any]] = [
            {"name": "retain", "description": self.retain.__doc__ or "", "function": self.retain},
            {"name": "recall", "description": self.recall.__doc__ or "", "function": self.recall},
        ]
        if self._include_reflect:
            fns.append({"name": "reflect", "description": self.reflect.__doc__ or "", "function": self.reflect})
        if self._include_forget:
            fns.append({"name": "forget", "description": self.forget.__doc__ or "", "function": self.forget})
        return fns
