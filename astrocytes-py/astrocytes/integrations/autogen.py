"""AutoGen / AG2 integration — Astrocytes memory for multi-agent conversations.

Usage:
    from astrocytes import Astrocyte
    from astrocytes.integrations.autogen import AstrocyteAutoGenMemory

    brain = Astrocyte.from_config("astrocytes.yaml")
    memory = AstrocyteAutoGenMemory(brain, bank_id="team-agents")

    # Register with AutoGen agent
    agent = ConversableAgent("assistant", llm_config=llm_config)
    # Use memory in conversation hooks
    await memory.save("User prefers Python", agent_id="assistant")
    context = await memory.get_context("What does the user prefer?")

Also provides tool registration for AutoGen's function-calling pattern:
    tools = memory.as_tools()
    agent.register_for_llm(tools)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocytes._astrocyte import Astrocyte


class AstrocyteAutoGenMemory:
    """Astrocytes-backed memory for AutoGen / AG2 agents.

    Provides both a direct API (save/query/get_context) and tool registration
    for AutoGen's function-calling pattern.
    """

    def __init__(
        self,
        brain: Astrocyte,
        bank_id: str,
        *,
        agent_banks: dict[str, str] | None = None,
    ) -> None:
        self.brain = brain
        self.bank_id = bank_id
        self._agent_banks = agent_banks or {}

    def _resolve_bank(self, agent_id: str | None = None) -> str:
        if agent_id and agent_id in self._agent_banks:
            return self._agent_banks[agent_id]
        return self.bank_id

    async def save(
        self,
        content: str,
        *,
        agent_id: str | None = None,
        tags: list[str] | None = None,
    ) -> str | None:
        """Save content to memory. Returns memory_id."""
        bank = self._resolve_bank(agent_id)
        result = await self.brain.retain(
            content,
            bank_id=bank,
            tags=tags or ["autogen"],
            metadata={"source": "autogen", "agent_id": agent_id or ""},
        )
        return result.memory_id if result.stored else None

    async def query(
        self,
        query: str,
        *,
        agent_id: str | None = None,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Query memory. Returns list of hit dicts."""
        bank = self._resolve_bank(agent_id)
        result = await self.brain.recall(query, bank_id=bank, max_results=max_results)
        return [{"text": h.text, "score": h.score, "memory_id": h.memory_id} for h in result.hits]

    async def get_context(
        self,
        query: str,
        *,
        agent_id: str | None = None,
        max_results: int = 5,
    ) -> str:
        """Get formatted memory context for injection into agent messages."""
        hits = await self.query(query, agent_id=agent_id, max_results=max_results)
        if not hits:
            return ""
        return "\n".join(f"- {h['text']}" for h in hits)

    def as_tools(self, *, include_reflect: bool = True) -> list[dict[str, Any]]:
        """Return OpenAI-format tool definitions for AutoGen function calling.

        AutoGen uses OpenAI-compatible tool definitions internally.
        """
        from astrocytes.integrations.openai_agents import astrocyte_tool_definitions

        tools, _handlers = astrocyte_tool_definitions(self.brain, self.bank_id, include_reflect=include_reflect)
        return tools

    def get_handlers(self, *, include_reflect: bool = True) -> dict[str, Any]:
        """Return handler functions for dispatching tool calls."""
        from astrocytes.integrations.openai_agents import astrocyte_tool_definitions

        _tools, handlers = astrocyte_tool_definitions(self.brain, self.bank_id, include_reflect=include_reflect)
        return handlers
