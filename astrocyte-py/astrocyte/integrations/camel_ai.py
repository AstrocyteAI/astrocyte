"""CAMEL-AI integration — Astrocyte memory for multi-agent role-playing.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.camel_ai import AstrocyteCamelMemory

    brain = Astrocyte.from_config("astrocyte.yaml")
    memory = AstrocyteCamelMemory(brain, bank_id="simulation")

    # Use with CAMEL agents
    memory.write("The patient reports headaches", role="doctor", agent_id="agent-1")
    context = await memory.read("patient symptoms", role="doctor")

CAMEL-AI uses role-playing conversations between agents. Each agent has a role
and memories are scoped by role and/or agent identity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


class AstrocyteCamelMemory:
    """Astrocyte-backed memory for CAMEL-AI multi-agent systems.

    Supports role-based memory scoping: each role can have its own bank
    or share a common bank. Memory is tagged with role and agent_id for filtering.
    """

    def __init__(
        self,
        brain: Astrocyte,
        bank_id: str,
        *,
        role_banks: dict[str, str] | None = None,
    ) -> None:
        self.brain = brain
        self.bank_id = bank_id
        self._role_banks = role_banks or {}

    def _resolve_bank(self, role: str | None = None) -> str:
        if role and role in self._role_banks:
            return self._role_banks[role]
        return self.bank_id

    async def write(
        self,
        content: str,
        *,
        role: str | None = None,
        agent_id: str | None = None,
        tags: list[str] | None = None,
    ) -> str | None:
        """Write to memory. Returns memory_id."""
        bank = self._resolve_bank(role)
        meta: dict[str, Any] = {"source": "camel-ai"}
        if role:
            meta["role"] = role
        if agent_id:
            meta["agent_id"] = agent_id

        all_tags = list(tags or [])
        all_tags.append("camel-ai")
        if role:
            all_tags.append(f"role:{role}")

        result = await self.brain.retain(content, bank_id=bank, tags=all_tags, metadata=meta)
        return result.memory_id if result.stored else None

    async def read(
        self,
        query: str,
        *,
        role: str | None = None,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Read from memory. Returns list of hit dicts."""
        bank = self._resolve_bank(role)
        result = await self.brain.recall(query, bank_id=bank, max_results=max_results)
        return [
            {"text": h.text, "score": h.score, "metadata": h.metadata, "memory_id": h.memory_id} for h in result.hits
        ]

    async def get_context(self, query: str, *, role: str | None = None, max_results: int = 5) -> str:
        """Get formatted memory context for injection into agent messages."""
        hits = await self.read(query, role=role, max_results=max_results)
        if not hits:
            return ""
        return "\n".join(f"- {h['text']}" for h in hits)

    async def reflect(self, query: str, *, role: str | None = None) -> str:
        """Synthesize an answer from memory."""
        bank = self._resolve_bank(role)
        result = await self.brain.reflect(query, bank_id=bank)
        return result.answer

    async def clear(self, *, role: str | None = None) -> None:
        """Clear all memory for a role or the shared bank."""
        bank = self._resolve_bank(role)
        from astrocyte.types import ForgetRequest

        await self.brain._do_forget(ForgetRequest(bank_id=bank, scope="all"))
