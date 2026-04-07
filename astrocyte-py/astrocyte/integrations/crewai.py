"""CrewAI integration — Astrocyte as crew/agent memory.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.crewai import AstrocyteCrewMemory

    brain = Astrocyte.from_config("astrocyte.yaml")

    crew = Crew(
        agents=[support_agent, research_agent],
        memory=AstrocyteCrewMemory(brain, bank_id="team-support"),
    )

Maps:
    - save → brain.retain()
    - search → brain.recall()
    - Crew-level bank for shared memory; per-agent banks via agent_banks
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


class AstrocyteCrewMemory:
    """Astrocyte-backed memory for CrewAI crews and agents.

    Implements the interface pattern expected by CrewAI's memory system:
    save(), search(), reset().

    Thin wrapper — all policy enforcement happens inside Astrocyte.
    """

    def __init__(
        self,
        brain: Astrocyte,
        bank_id: str,
        *,
        agent_banks: dict[str, str] | None = None,
        auto_retain: bool = False,
    ) -> None:
        self.brain = brain
        self.bank_id = bank_id
        self._agent_banks = agent_banks or {}
        self.auto_retain = auto_retain

    def _resolve_bank(self, agent_id: str | None = None) -> str:
        """Per-agent bank or shared crew bank."""
        if agent_id and agent_id in self._agent_banks:
            return self._agent_banks[agent_id]
        return self.bank_id

    async def save(
        self,
        content: str,
        *,
        agent_id: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Save content to memory (retain)."""
        bank = self._resolve_bank(agent_id)
        meta = dict(metadata or {})
        meta["source"] = "crewai"
        if agent_id:
            meta["agent_id"] = agent_id

        await self.brain.retain(
            content,
            bank_id=bank,
            tags=tags or ["crewai"],
            metadata=meta,
        )

    async def search(
        self,
        query: str,
        *,
        agent_id: str | None = None,
        max_results: int = 5,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search memory (recall). Returns list of dicts."""
        bank = self._resolve_bank(agent_id)
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

    async def reset(self, *, agent_id: str | None = None) -> None:
        """Reset memory for a bank (forget all)."""
        bank = self._resolve_bank(agent_id)
        await self.brain.clear_bank(bank)
