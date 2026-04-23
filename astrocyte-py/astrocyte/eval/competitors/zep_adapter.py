"""Zep brain adapter — presents the Zep SDK as an Astrocyte-shaped
brain so the LoCoMo / LongMemEval benchmark adapters can run unchanged.

Scope:

- ``retain`` → ``Memory.add_session`` + ``Memory.add`` (Zep's primitives
  for adding messages to a session's memory)
- ``recall`` → ``Memory.search_sessions`` (semantic retrieval)
- ``reflect`` → composed from recall + LLM synthesis (Zep Cloud exposes
  a ``Memory.synthesize`` primitive but OSS does not; we synthesize
  using the same LLM path as Mem0 so the comparison is consistent)

Requires: ``pip install astrocyte[competitors]`` (installs ``zep-python``)
plus ``ZEP_API_KEY`` env var (Zep Cloud) or ``ZEP_API_URL`` for
self-hosted. The Doppler ``prd`` config in Astrocyte's setup should
carry ``ZEP_API_KEY`` when a Zep benchmark run is queued.

Bank model: Zep scopes memories by ``session_id``. We map
``bank_id → session_id`` 1:1. Benchmarks use single-bank configurations
so this is lossless; multi-bank comparison would need a session-per-bank
naming convention (e.g. ``f"{run_id}:{bank_id}"``).

Client threading model: same as the Mem0 adapter — sync ``ZepClient``
wrapped with ``asyncio.to_thread``; async ``AsyncZepClient`` awaited
directly via ``asyncio.iscoroutinefunction``.

This module is a **scaffold** — method bodies that call the SDK are
marked with explicit TODO comments. The interface, error handling,
bank-mapping convention, and docstrings are production-ready; the SDK
calls get filled in when we commit to running the Zep head-to-head
(after Mem0 baseline is validated and canonical F1 numbers are locked).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from astrocyte.types import RecallResult, ReflectResult, RetainResult

if TYPE_CHECKING:
    from astrocyte.eval.competitors._base import _PipelineLike

_logger = logging.getLogger("astrocyte.eval.competitors.zep")


class ZepBrainAdapter:
    """Wraps Zep to present the minimal Astrocyte brain surface.

    Bank model: Zep uses ``session_id`` to scope memories. Astrocyte's
    benchmark adapters pass ``bank_id`` to every call; we map
    ``bank_id → session_id`` 1:1.

    Example:

        from zep_python import ZepClient
        client = ZepClient(api_key=os.environ["ZEP_API_KEY"])
        brain = ZepBrainAdapter(client, llm_provider=my_litellm_provider)
        # Pass to LoComoBenchmark(brain) as usual.
    """

    def __init__(
        self,
        client: Any,
        *,
        llm_provider: Any = None,
    ) -> None:
        """Construct the adapter.

        Args:
            client: A ``zep_python.ZepClient`` (or ``AsyncZepClient``)
                instance, fully configured with API key. The adapter
                does not create one for you.
            llm_provider: An ``astrocyte.provider.LLMProvider`` used for
                the ``reflect`` synthesis pass and for the LongMemEval
                canonical judge. Same model family as Zep uses for
                extraction keeps the comparison model-apples-to-apples.
        """
        self._client = client
        self._pipeline: _PipelineLike | None = (
            _LightPipeline(llm_provider) if llm_provider else None
        )

    async def retain(
        self,
        content: str,
        *,
        bank_id: str,
        metadata: dict | None = None,
        tags: list[str] | None = None,
        occurred_at: datetime | None = None,
        content_type: str = "text",  # noqa: ARG002 — Zep ignores content_type
    ) -> RetainResult:
        """Store ``content`` into Zep under ``session_id=bank_id``.

        Zep's ``Memory.add`` accepts a ``Memory`` object containing a list
        of messages. We wrap the content as a single ``Message`` with role
        ``"user"`` (Zep's convention for user-generated content).

        Zep Cloud supports ``created_at`` on individual messages for
        temporal ordering; OSS ignores it. We pass it when present so
        Cloud runs get accurate temporal metadata.
        """
        # TODO: real implementation:
        #
        #   from zep_python.memory import Memory, Message
        #   msg = Message(role="user", content=content, metadata=metadata or {})
        #   if occurred_at is not None:
        #       msg.created_at = occurred_at.isoformat()
        #   await _call(self._client.memory.add, bank_id, Memory(messages=[msg]))
        #   return RetainResult(stored=True)
        #
        # Ensure the session exists first (Zep raises if session is missing):
        #   await _call(self._client.memory.add_session, Session(session_id=bank_id))
        # (add_session is idempotent in Zep Cloud ≥0.26.)
        raise NotImplementedError(
            "ZepBrainAdapter.retain — wire self._client.memory.add() here. "
            "See SDK docs: https://github.com/getzep/zep-python",
        )

    async def recall(
        self,
        query: str,
        *,
        bank_id: str,
        max_results: int = 10,
    ) -> RecallResult:
        """Semantic retrieve via ``Memory.search_sessions``.

        Zep returns ``SearchResult`` objects with ``message`` (the stored
        content) and ``score`` (relevance). We map these to ``MemoryHit``
        so downstream metric code reads ``.text`` and ``.memory_id``
        without caring which system produced them.
        """
        # TODO: real implementation:
        #
        #   from zep_python.memory import MemorySearchPayload
        #   payload = MemorySearchPayload(text=query, limit=max_results)
        #   results = await _call(self._client.memory.search_memory, bank_id, payload)
        #   hits = [
        #       MemoryHit(
        #           text=r.message.content if r.message else "",
        #           score=float(r.score or 0.0),
        #           memory_id=r.message.uuid if r.message else None,
        #           metadata=r.message.metadata if r.message else None,
        #       )
        #       for r in (results or [])
        #   ]
        #   return RecallResult(hits=hits, total_available=len(hits), truncated=False)
        raise NotImplementedError(
            "ZepBrainAdapter.recall — wire self._client.memory.search_memory() here.",
        )

    async def reflect(
        self,
        query: str,
        *,
        bank_id: str,
        max_tokens: int | None = None,
    ) -> ReflectResult:
        """Synthesize an answer by retrieving then prompting the LLM.

        Zep Cloud exposes a ``Memory.synthesize`` primitive (session
        summarisation + QA). OSS does not. For a consistent comparison
        matrix we synthesize here using ``astrocyte.pipeline.reflect``
        — the same prompt path as Mem0 — so the Astrocyte-vs-Zep delta
        isolates memory retrieval quality, not synthesis differences.

        When Zep Cloud's native synthesize is preferred, swap this body
        for a direct ``self._client.memory.synthesize(session_id, query)``
        call and document the difference in the benchmark README.
        """
        if self._pipeline is None or self._pipeline.llm_provider is None:
            raise RuntimeError(
                "ZepBrainAdapter.reflect needs an llm_provider — pass one to "
                "the constructor. Without it LoCoMo can run with "
                "use_canonical_judge=False only; LongMemEval requires reflect.",
            )

        from astrocyte.pipeline.reflect import synthesize

        recall_result = await self.recall(query, bank_id=bank_id, max_results=10)
        return await synthesize(
            query,
            recall_result.hits,
            self._pipeline.llm_provider,
            max_tokens=max_tokens or 2048,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call ``fn(*args, **kwargs)`` without blocking the event loop."""
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


class _LightPipeline:
    def __init__(self, llm_provider: Any) -> None:
        self.llm_provider = llm_provider

    def reset_token_counter(self) -> int:
        return 0


def _lazy_import_zep() -> Any:
    try:
        import zep_python
    except ImportError as exc:
        raise ImportError(
            "zep-python not installed. Install with: pip install 'astrocyte[competitors]' "
            "(or directly: pip install zep-python). Ensure ZEP_API_KEY (Cloud) or "
            "ZEP_API_URL (self-hosted) is set before running benchmarks.",
        ) from exc
    return zep_python
