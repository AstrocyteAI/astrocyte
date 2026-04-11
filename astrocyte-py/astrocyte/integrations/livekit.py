"""LiveKit Agents integration — Astrocyte memory for real-time voice/video agents.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.livekit import AstrocyteLiveKitMemory

    brain = Astrocyte.from_config("astrocyte.yaml")
    memory = AstrocyteLiveKitMemory(brain, bank_id="session-123")

    # In a LiveKit agent session handler
    async def on_session(ctx: AgentContext):
        # Recall relevant context for the conversation
        context = await memory.get_session_context("user preferences")

        # After the conversation, retain key information
        await memory.retain_from_session(
            "User mentioned they prefer morning appointments",
            session_id=ctx.session_id,
        )

LiveKit Agents are real-time voice/video agents. Memory integration is
different from batch agents — it's about:
1. Loading context at session start (pre-fetch relevant memories)
2. Retaining key information after sessions (post-session summarization)
3. Mid-session recall for long-running conversations
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte

from astrocyte.types import AstrocyteContext


class AstrocyteLiveKitMemory:
    """Astrocyte memory adapter for LiveKit real-time agents.

    Designed for the real-time voice/video agent lifecycle:
    - Session start: load relevant memories as context
    - Mid-session: recall for dynamic context enrichment
    - Session end: retain key takeaways
    """

    def __init__(
        self,
        brain: Astrocyte,
        bank_id: str,
        *,
        context: AstrocyteContext | None = None,
        session_bank_prefix: str | None = None,
        max_context_items: int = 10,
    ) -> None:
        self.brain = brain
        self.bank_id = bank_id
        self._context = context
        self._session_prefix = session_bank_prefix
        self.max_context_items = max_context_items

    def _session_bank(self, session_id: str | None) -> str:
        """Per-session bank or shared bank."""
        if session_id and self._session_prefix:
            return f"{self._session_prefix}{session_id}"
        return self.bank_id

    async def get_session_context(
        self,
        query: str,
        *,
        session_id: str | None = None,
        max_results: int | None = None,
    ) -> str:
        """Load relevant memories as context for a session start.

        Returns formatted text suitable for injection into a system prompt.
        """
        bank = self._session_bank(session_id)
        result = await self.brain.recall(
            query,
            bank_id=bank,
            max_results=max_results or self.max_context_items,
            context=self._context,
        )
        if not result.hits:
            return ""
        return "\n".join(f"- {h.text}" for h in result.hits)

    async def recall_mid_session(
        self,
        query: str,
        *,
        session_id: str | None = None,
        max_results: int = 3,
    ) -> list[dict[str, Any]]:
        """Quick recall during an active session for context enrichment.

        Returns structured results for programmatic use.
        """
        bank = self._session_bank(session_id)
        result = await self.brain.recall(query, bank_id=bank, max_results=max_results, context=self._context)
        return [{"text": h.text, "score": h.score, "memory_id": h.memory_id} for h in result.hits]

    async def retain_from_session(
        self,
        content: str,
        *,
        session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> str | None:
        """Retain key information from a session. Returns memory_id."""
        bank = self._session_bank(session_id)
        meta: dict[str, Any] = {"source": "livekit"}
        if session_id:
            meta["session_id"] = session_id

        all_tags = list(tags or [])
        all_tags.append("livekit")

        result = await self.brain.retain(content, bank_id=bank, tags=all_tags, metadata=meta, context=self._context)
        return result.memory_id if result.stored else None

    async def summarize_session(
        self,
        query: str = "Summarize the key points from this session",
        *,
        session_id: str | None = None,
    ) -> str:
        """Use reflect to synthesize session memories into a summary."""
        bank = self._session_bank(session_id)
        result = await self.brain.reflect(query, bank_id=bank, context=self._context)
        return result.answer
