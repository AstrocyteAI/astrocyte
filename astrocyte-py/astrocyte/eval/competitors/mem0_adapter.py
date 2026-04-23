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

This module is a **scaffold** — method bodies that call the SDK are
marked with explicit TODO comments. The interface, error handling,
bank-mapping convention, and docstrings are production-ready; the SDK
calls get filled in when we commit to running the head-to-head matrix
(after v5 judge calibration lands).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from astrocyte.types import RecallResult, ReflectResult, RetainResult

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
        """
        # TODO: real implementation calls self._client.add(...)
        # metadata and tags map directly; occurred_at has no mem0
        # equivalent today (mem0 uses server-side timestamps) — log a
        # warning rather than raise, since the benchmark can still run.
        if occurred_at is not None:
            _logger.debug("Mem0 does not support occurred_at; ignoring")
        raise NotImplementedError(
            "Mem0BrainAdapter.retain — call self._client.add() here. "
            "See astrocyte/eval/competitors/README.md for the SDK-to-Astrocyte "
            "mapping. Mark as done after validating on a small smoke run.",
        )

    async def recall(
        self,
        query: str,
        *,
        bank_id: str,
        max_results: int = 10,
    ) -> RecallResult:
        """Semantic retrieve via ``Memory.search``."""
        # TODO: self._client.search(query=query, user_id=bank_id, limit=max_results)
        # Map each mem0 memory result to a MemoryHit. mem0 returns
        # {id, memory, score, metadata, ...}; benchmark metric code
        # uses MemoryHit.memory_id + text, so at minimum map those.
        raise NotImplementedError(
            "Mem0BrainAdapter.recall — call self._client.search() here.",
        )

    async def reflect(
        self,
        query: str,
        *,
        bank_id: str,
        max_tokens: int | None = None,
    ) -> ReflectResult:
        """Synthesize an answer by retrieving then prompting the LLM.

        Mem0 OSS does not ship a reflect primitive. We synthesize here
        so the comparison treats the entire retain → recall → reflect
        chain as a black box, matching how LoCoMo / LongMemEval evaluate
        end-to-end memory systems.
        """
        if self._pipeline is None or self._pipeline.llm_provider is None:
            raise RuntimeError(
                "Mem0BrainAdapter.reflect needs an llm_provider — pass one to "
                "the constructor for synthesis. Without it, the benchmark can "
                "still run LoCoMo with use_canonical_judge=False against "
                "recall hits only, but LongMemEval requires reflect output.",
            )
        # TODO:
        # 1. Await self.recall(query, bank_id=bank_id, max_results=10)
        # 2. Build a synthesis prompt from the recalled memories
        # 3. self._pipeline.llm_provider.complete([...])
        # 4. Wrap in ReflectResult(answer=..., sources=recall.hits)
        raise NotImplementedError(
            "Mem0BrainAdapter.reflect — synthesize from recall + LLM here. "
            "Reuse the prompt structure from "
            "astrocyte/pipeline/reflect.py::synthesize for consistency — that "
            "way the Astrocyte-vs-Mem0 delta isolates retrieval / memory "
            "layers, not synthesis-prompt differences.",
        )


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
