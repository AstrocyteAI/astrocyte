"""Zep brain adapter — presents the Zep SDK as an Astrocyte-shaped
brain so the LoCoMo / LongMemEval benchmark adapters can run unchanged.

Scope:

- ``retain`` → ``memory.add_memory`` / ``aadd_memory``
- ``recall`` → ``memory.search_memory`` / ``asearch_memory``
- ``reflect`` → recall + LLM synthesis via ``astrocyte.pipeline.reflect``
  (Zep Cloud has a native synthesize primitive, but for a fair cross-system
  comparison we use the same synthesis path as every other adapter so the
  delta isolates retrieval quality, not prompt differences)

Requires: ``pip install astrocyte[competitors]`` (installs ``zep-python``)
plus ``ZEP_API_KEY`` env var (Zep Cloud) or ``ZEP_API_URL`` for
self-hosted. The Doppler ``prd`` config carries ``ZEP_API_KEY`` for
production benchmark runs.

SDK version: targets zep-python v1.x (OSS / self-hosted). v2.x (Cloud)
uses ``Zep``/``AsyncZep`` with slightly different method names
(``memory.add`` / ``memory.search``); see inline notes where the diff
matters.

Bank model: Zep scopes memories by ``session_id``. We map
``bank_id → session_id`` 1:1. Benchmarks use single-bank configurations
so this is lossless; multi-bank comparison would need a per-bank
session-ID prefix convention (e.g. ``f"{run_id}:{bank_id}"``).

Session lifecycle: Zep raises ``NotFoundError`` when adding to a
non-existent session. The adapter creates sessions lazily on first
retain and caches the session IDs so repeated retain calls don't pay
the creation round-trip. The cache is per-adapter-instance; long-lived
adapters sharing a session namespace should pass pre-created sessions.

Client threading: sync ``ZepClient`` is wrapped with
``asyncio.to_thread`` so it doesn't block the event loop during
benchmark runs. Pass an async client (``AsyncZepClient`` in v1,
``AsyncZep`` in v2) to await SDK calls directly; the adapter detects
which variant via ``asyncio.iscoroutinefunction``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from astrocyte.types import MemoryHit, RecallResult, ReflectResult, RetainResult

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
        client = ZepClient(base_url="http://localhost:8000")
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
                instance, fully configured with API key / base URL. The
                adapter does not create one for you.
            llm_provider: An ``astrocyte.provider.LLMProvider`` used for
                the ``reflect`` synthesis pass and for the LongMemEval
                canonical judge. Same model family as Zep uses for
                extraction keeps the comparison model-apples-to-apples.
        """
        self._client = client
        self._pipeline: _PipelineLike | None = (
            _LightPipeline(llm_provider) if llm_provider else None
        )
        # Lazily-created session IDs. Prevents duplicate add_session calls
        # while keeping the adapter stateless across banks.
        self._sessions: set[str] = set()

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

        Wraps the content as a single ``Message`` with role ``"user"``.
        Ensures the session exists (idempotent ``add_session``) before
        calling ``add_memory``, caching the session ID to avoid repeated
        creation round-trips.

        ``occurred_at``: mapped to ``Message.created_at`` (ISO string).
        Zep Cloud stores it for temporal ordering; OSS ignores the field.
        ``tags`` and non-string ``metadata`` values are merged into
        ``Message.metadata`` since Zep has no native tag primitive.
        """
        from zep_python.memory import Memory, Message

        await self._ensure_session(bank_id)

        merged_meta: dict = dict(metadata or {})
        if tags:
            merged_meta["tags"] = tags

        msg_kwargs: dict[str, Any] = {
            "role": "user",
            "content": content,
        }
        if merged_meta:
            msg_kwargs["metadata"] = merged_meta
        if occurred_at is not None:
            # created_at is a Zep Cloud extension; OSS Message silently
            # ignores unknown kwargs, so this is safe across versions.
            msg_kwargs["created_at"] = occurred_at.isoformat()

        msg = Message(**msg_kwargs)
        memory = Memory(messages=[msg])

        try:
            await _call(self._client.memory.add_memory, bank_id, memory)
        except Exception as exc:
            _logger.warning("Zep retain failed for bank=%s: %s", bank_id, exc)
            return RetainResult(stored=False, error=str(exc))

        return RetainResult(stored=True)

    async def recall(
        self,
        query: str,
        *,
        bank_id: str,
        max_results: int = 10,
    ) -> RecallResult:
        """Semantic retrieve via ``memory.search_memory``.

        Zep returns a list of ``MemorySearchResult`` objects; each has
        a ``.message`` (``Message``) and ``.score`` (float). We map to
        ``MemoryHit`` so downstream metric code is system-agnostic.

        If the session doesn't exist yet (first recall before any
        retain), returns empty hits rather than raising.
        """
        from zep_python.memory import MemorySearchPayload

        payload = MemorySearchPayload(text=query, limit=max_results)

        try:
            raw = await _call(
                self._client.memory.search_memory, bank_id, payload, max_results
            )
        except Exception as exc:
            # Session may not exist yet (e.g. recall before any retain in
            # the benchmark warm-up). Return empty rather than crashing the run.
            _logger.debug(
                "Zep recall returned no results for bank=%s: %s", bank_id, exc
            )
            return RecallResult(hits=[], total_available=0, truncated=False)

        hits: list[MemoryHit] = []
        for r in raw or []:
            msg = getattr(r, "message", None)
            if msg is None:
                continue
            hits.append(
                MemoryHit(
                    text=getattr(msg, "content", "") or "",
                    score=float(getattr(r, "score", 0.0) or 0.0),
                    memory_id=getattr(msg, "uuid", None),
                    metadata=getattr(msg, "metadata", None) or None,
                )
            )

        return RecallResult(hits=hits, total_available=len(hits), truncated=False)

    async def reflect(
        self,
        query: str,
        *,
        bank_id: str,
        max_tokens: int | None = None,
    ) -> ReflectResult:
        """Synthesize an answer by retrieving then prompting the LLM.

        We use the same ``astrocyte.pipeline.reflect.synthesize`` path as
        the Mem0 adapter so the Astrocyte-vs-Zep comparison isolates
        memory-retrieval quality, not synthesis-prompt differences.

        Note: Zep Cloud exposes a ``memory.synthesize(session_id, query)``
        primitive. If you prefer that for Cloud runs, replace this body
        with a direct SDK call and document it in the benchmark README
        (the numbers won't be directly comparable to the shared-LLM matrix).
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
    # Internal helpers
    # ---------------------------------------------------------------------------

    async def _ensure_session(self, bank_id: str) -> None:
        """Create ``bank_id`` as a Zep session if not already cached."""
        if bank_id in self._sessions:
            return
        from zep_python.memory import Session

        try:
            await _call(
                self._client.memory.add_session,
                Session(session_id=bank_id),
            )
        except Exception as exc:
            # add_session is idempotent in ≥0.26 and raises a conflict
            # error in older versions when the session already exists.
            # Either way, we can proceed — the session is there.
            _logger.debug(
                "add_session for bank=%s raised (may already exist): %s",
                bank_id, exc,
            )
        self._sessions.add(bank_id)


# ---------------------------------------------------------------------------
# Module-level helpers
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
