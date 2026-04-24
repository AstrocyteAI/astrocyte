"""Mem0 brain adapter — presents the mem0ai SDK as an Astrocyte-shaped
brain so the LoCoMo / LongMemEval benchmark adapters can run unchanged.

Scope:

- ``retain`` → ``Memory.add`` (mem0's primitive for storing memories)
- ``recall`` → ``Memory.search`` (semantic retrieval)
- ``reflect`` → composed from recall + an LLM call against mem0's
  configured model (mem0 doesn't ship a reflect primitive; we
  synthesize using the same LLM mem0 uses for extraction, so the
  comparison stays model-apples-to-apples)

Requires: ``pip install astrocyte[competitors]`` (installs ``mem0ai``)
plus ``MEM0_API_KEY`` env var (Mem0 Cloud) or ``MEM0_CONFIG`` for
self-hosted. The Doppler ``prd`` config in Astrocyte's setup should
carry ``MEM0_API_KEY`` when a Mem0 benchmark run is queued.

Client threading model: the adapter wraps the *sync* ``mem0.Memory``
client with ``asyncio.to_thread`` so it works inside the async
benchmark loop without blocking the event loop. Pass ``AsyncMemory``
if you need true async fan-out — the adapter detects coroutines and
awaits them directly.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from astrocyte.types import MemoryHit, RecallResult, ReflectResult, RetainResult

if TYPE_CHECKING:
    from astrocyte.eval.competitors._base import _PipelineLike

_logger = logging.getLogger("astrocyte.eval.competitors.mem0")


class Mem0BrainAdapter:
    """Wraps Mem0 to present the minimal Astrocyte brain surface.

    Bank model: Mem0 uses ``user_id`` to scope memories. Astrocyte's
    benchmark adapters pass ``bank_id`` to every call; we map
    ``bank_id → user_id`` 1:1. Benchmarks use single-bank configurations
    so this is lossless; multi-bank comparison would need a different
    mapping strategy.

    Example:

        from mem0 import Memory
        client = Memory.from_config({...})
        brain = Mem0BrainAdapter(client, llm_provider=my_litellm_provider)
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
            client: A ``mem0.Memory`` (or ``AsyncMemory``) instance, fully
                configured with API key and model choice. The adapter
                does not create one for you — that's the caller's job so
                deployment-specific settings (API endpoint, local
                inference, custom vectorstore) stay in the caller's
                hands.
            llm_provider: An ``astrocyte.provider.LLMProvider`` used for
                the ``reflect`` synthesis pass and for the LongMemEval
                canonical judge. Should be configured with the same
                underlying model family Mem0 uses, so the comparison
                isolates memory-layer quality from model-layer quality.
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
        content_type: str = "text",  # noqa: ARG002 — mem0 ignores content_type
    ) -> RetainResult:
        """Store ``content`` into mem0 under ``user_id=bank_id``.

        Mem0's ``Memory.add`` accepts a list of messages; we wrap the
        single content string in a single-message list using the
        ``"user"`` role as mem0's examples do.

        ``occurred_at`` has no mem0 equivalent — mem0 uses server-side
        timestamps. We log at DEBUG and continue; the benchmark run is
        still valid, we just lose the temporal metadata for that entry.
        """
        if occurred_at is not None:
            _logger.debug(
                "Mem0 does not support occurred_at (bank=%s); ignoring", bank_id
            )

        meta: dict = dict(metadata or {})
        if tags:
            meta["tags"] = tags

        raw = await _call(
            self._client.add,
            [{"role": "user", "content": content}],
            user_id=bank_id,
            **({"metadata": meta} if meta else {}),
        )

        # mem0 ≥0.1.x returns {"results": [{"id": "...", "event": "ADD", ...}]}
        # Older versions may return a flat list — handle both.
        memory_id: str | None = None
        results_list: list = []
        if isinstance(raw, dict):
            results_list = raw.get("results", [])
        elif isinstance(raw, list):
            results_list = raw
        if results_list:
            memory_id = results_list[0].get("id")

        return RetainResult(stored=True, memory_id=memory_id)

    async def recall(
        self,
        query: str,
        *,
        bank_id: str,
        max_results: int = 10,
    ) -> RecallResult:
        """Semantic retrieve via ``Memory.search``.

        mem0 returns ``{"results": [{"id": ..., "memory": ..., "score": ...}]}``
        (≥0.1.x) or a flat list. We normalise to ``RecallResult`` so the
        benchmark metric code reads ``hits[i].text`` / ``hits[i].memory_id``
        without caring which system produced them.
        """
        raw = await _call(
            self._client.search,
            query=query,
            user_id=bank_id,
            limit=max_results,
        )

        raw_list: list = []
        if isinstance(raw, dict):
            raw_list = raw.get("results", [])
        elif isinstance(raw, list):
            raw_list = raw

        hits = [
            MemoryHit(
                text=r.get("memory", ""),
                score=float(r.get("score", 0.0)),
                memory_id=r.get("id"),
                metadata=r.get("metadata") or None,
            )
            for r in raw_list
        ]
        return RecallResult(hits=hits, total_available=len(hits), truncated=False)

    async def reflect(
        self,
        query: str,
        *,
        bank_id: str,
        max_tokens: int | None = None,
    ) -> ReflectResult:
        """Synthesize an answer by retrieving then prompting the LLM.

        Mem0 OSS does not ship a reflect primitive. We synthesize here
        using ``astrocyte.pipeline.reflect.synthesize`` — the same
        prompt and formatting path Astrocyte uses — so the comparison
        isolates memory retrieval quality from synthesis-prompt
        differences.
        """
        if self._pipeline is None or self._pipeline.llm_provider is None:
            raise RuntimeError(
                "Mem0BrainAdapter.reflect needs an llm_provider — pass one to "
                "the constructor for synthesis. Without it the benchmark can "
                "still run LoCoMo with use_canonical_judge=False against "
                "recall hits only, but LongMemEval requires reflect output.",
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
    """Call ``fn(*args, **kwargs)`` without blocking the event loop.

    ``AsyncMemory`` methods are coroutine functions — detected via
    ``asyncio.iscoroutinefunction`` and awaited directly.
    Sync ``Memory`` methods run in ``asyncio.to_thread`` so they don't
    stall the event loop during benchmark runs.
    """
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


class _LightPipeline:
    """Minimal pipeline shim for competitor adapters — satisfies the
    ``_pipeline.llm_provider`` / ``_pipeline.reset_token_counter``
    hooks that benchmark adapters touch, without needing Astrocyte's
    full orchestrator."""

    def __init__(self, llm_provider: Any) -> None:
        self.llm_provider = llm_provider

    def reset_token_counter(self) -> int:
        return 0


# Convenience — for benchmark runner to resolve the adapter by name.

def _lazy_import_mem0() -> Any:
    try:
        import mem0
    except ImportError as exc:
        raise ImportError(
            "mem0ai not installed. Install with: pip install 'astrocyte[competitors]' "
            "(or directly: pip install mem0ai). Ensure MEM0_API_KEY is set "
            "(via Doppler in production) before running benchmarks.",
        ) from exc
    return mem0
