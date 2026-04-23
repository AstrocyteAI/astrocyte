"""Shared protocol + factory for competitor brain adapters.

Defines exactly the brain surface that :class:`LoCoMoBenchmark` and
:class:`LongMemEvalBenchmark` depend on — nothing more, nothing less.
Competitor adapters implement this Protocol to pass duck-type checks;
the benchmark adapters never reach for Astrocyte-specific attributes
(MIP router, lifecycle manager, policy layer) on the brain.

The factory :func:`build_competitor_brain` is the single entry point
``scripts/run_benchmarks.py --competitor <name>`` uses. New competitors
register here.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider
    from astrocyte.types import RecallResult, ReflectResult


class CompetitorNotConfigured(RuntimeError):
    """The competitor's SDK / credentials / SDK-specific config is missing.

    Raised at adapter construction time with a clear message about what
    needs wiring. Deliberately a subclass of ``RuntimeError`` (not
    ``ImportError``) so the benchmark harness can decide whether to
    skip the run or fail the matrix — typically skip + warn so one
    misconfigured competitor doesn't block the rest.
    """


@runtime_checkable
class _PipelineHandle(Protocol):
    """Internal — the small ``brain._pipeline`` shim the LongMemEval
    benchmark reaches into for the reflect-time LLM provider."""

    llm_provider: LLMProvider


@runtime_checkable
class CompetitorBrain(Protocol):
    """Minimal brain surface the benchmark adapters depend on.

    Any object matching this protocol can be passed to
    :class:`LoCoMoBenchmark` or :class:`LongMemEvalBenchmark` in place
    of a full :class:`astrocyte.Astrocyte` instance. The protocol is
    intentionally narrow — three async methods plus one attribute —
    so competitor shims stay small and the fair-comparison story is
    audit-able.
    """

    # Benchmark adapters reach for this when they need to hand the LLM
    # provider to the canonical LongMemEval LLM-judge. Competitor
    # adapters expose a stub with the judge's llm_provider wired, so
    # all systems are scored with the same judge LLM.
    _pipeline: _PipelineHandle | None

    async def retain(
        self,
        content: str,
        *,
        bank_id: str,
        metadata: dict | None = None,
        tags: list[str] | None = None,
        occurred_at: datetime | None = None,
        content_type: str = "text",
    ) -> object:
        """Store content. Return value is ignored by the benchmarks;
        they only care about eventual recall-ability. Competitor adapters
        return whatever their SDK returns — the benchmarks don't parse it.
        """
        ...

    async def recall(
        self,
        query: str,
        *,
        bank_id: str,
        max_results: int = 10,
    ) -> RecallResult:
        """Retrieve memories relevant to ``query`` from ``bank_id``.

        Competitor adapters MUST shape the return value into
        :class:`astrocyte.types.RecallResult` so the benchmark scorers
        can read ``hits[i].text`` and ``hits[i].memory_id`` without
        caring which system produced them.
        """
        ...

    async def reflect(
        self,
        query: str,
        *,
        bank_id: str,
        max_tokens: int | None = None,
    ) -> ReflectResult:
        """Synthesize an answer to ``query`` from memories in ``bank_id``.

        Competitors often don't expose this as a primitive (Mem0 OSS
        doesn't, for example). The adapter is free to implement reflect
        as ``recall → own LLM call`` using the same llm_provider we pass
        in. The fair-comparison contract is: **same LLM does all
        synthesis in the matrix**, regardless of which brain retrieved
        the chunks.
        """
        ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_competitor_brain(
    name: str,
    *,
    llm_provider: LLMProvider,
    config: dict | None = None,
) -> CompetitorBrain:
    """Construct a competitor adapter by name.

    Args:
        name: One of ``"mem0"``, ``"zep"``. Case-insensitive. Raises
            :class:`ValueError` for unknown names — the CLI surface
            should validate before calling.
        llm_provider: LLM used for reflect synthesis AND for the
            canonical LongMemEval LLM-judge. Same provider across all
            systems in the matrix so synthesis quality is held constant.
        config: Optional per-adapter config dict (API keys from env,
            endpoint overrides, bank-id prefix conventions). Each
            adapter documents its own keys.

    Lazy-imports the adapter module so unused competitors don't pull
    their SDKs into ``astrocyte`` startup.
    """
    key = name.strip().lower()
    config = config or {}

    if key == "mem0":
        from astrocyte.eval.competitors.mem0_brain import Mem0Brain

        return Mem0Brain(llm_provider=llm_provider, **config)

    if key == "zep":
        from astrocyte.eval.competitors.zep_brain import ZepBrain

        return ZepBrain(llm_provider=llm_provider, **config)

    raise ValueError(
        f"Unknown competitor: {name!r}. "
        f"Known: {sorted(_KNOWN_COMPETITORS)}.",
    )


#: Used by :func:`build_competitor_brain` for error messages. Keep in
#: sync with the dispatch above.
_KNOWN_COMPETITORS: frozenset[str] = frozenset({"mem0", "zep"})
