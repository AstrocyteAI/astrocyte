"""DSPy integration — Astrocyte as a DSPy retrieval module.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.dspy import AstrocyteRM

    brain = Astrocyte.from_config("astrocyte.yaml")
    retriever = AstrocyteRM(brain, bank_id="knowledge-base")

    # Use as a DSPy retrieval model
    import dspy
    dspy.configure(rm=retriever)

    # Or use directly
    results = retriever("What is dark mode?", k=5)

DSPy uses retrieval models (RM) that implement __call__(query, k) → list[str].
The AstrocyteRM wraps brain.recall() to match this pattern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte

from astrocyte.integrations._sync_utils import _run_async_from_sync
from astrocyte.types import AstrocyteContext


class AstrocyteRM:
    """Astrocyte-backed retrieval model for DSPy.

    Implements DSPy's RM protocol: __call__(query, k) → list of passage strings.
    Also provides async methods for direct use.
    """

    def __init__(
        self,
        brain: Astrocyte,
        bank_id: str,
        *,
        context: AstrocyteContext | None = None,
        default_k: int = 5,
    ) -> None:
        self.brain = brain
        self.bank_id = bank_id
        self._context = context
        self.default_k = default_k

    def __call__(self, query: str, k: int | None = None) -> list[str]:
        """Synchronous retrieval (DSPy RM protocol).

        Returns list of passage strings.
        """
        k = k or self.default_k
        return _run_async_from_sync(self._retrieve(query, k))

    async def _retrieve(self, query: str, k: int) -> list[str]:
        result = await self.brain.recall(query, bank_id=self.bank_id, max_results=k, context=self._context)
        return [h.text for h in result.hits]

    async def aretrieve(self, query: str, k: int | None = None) -> list[str]:
        """Async retrieval for use in async DSPy pipelines."""
        return await self._retrieve(query, k or self.default_k)

    async def aretain(self, content: str, **kwargs: Any) -> str | None:
        """Store content for later retrieval. Returns memory_id."""
        ctx = kwargs.pop("context", self._context)
        result = await self.brain.retain(content, bank_id=self.bank_id, context=ctx, **kwargs)
        return result.memory_id if result.stored else None

    async def areflect(self, query: str) -> str:
        """Synthesize an answer from memory."""
        result = await self.brain.reflect(query, bank_id=self.bank_id, context=self._context)
        return result.answer
