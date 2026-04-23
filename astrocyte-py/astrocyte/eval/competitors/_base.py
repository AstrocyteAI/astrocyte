"""Common surface for competitor brain adapters.

The benchmark harness duck-types on a small subset of the Astrocyte
brain API. This module documents and pins that subset so competitor
adapters (Mem0, Zep, ...) know exactly what to implement.

We use ``Protocol`` rather than an ABC so existing Astrocyte code paths
(``astrocyte.Astrocyte``) are structurally compatible without inheriting
from this module — the base class would introduce a false import cycle
between the core and the eval subpackage.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from astrocyte.types import RecallResult, ReflectResult, RetainResult


class _PipelineLike(Protocol):
    """The pipeline attribute the benchmark adapters dip into.

    Only ``llm_provider`` is needed — it powers the LongMemEval canonical
    judge (which needs an LLM to run yes/no scoring prompts). Competitor
    adapters should expose their own LLM provider here so the judge
    uses the same model family they use for completions internally,
    keeping the comparison clean.
    """

    llm_provider: object

    def reset_token_counter(self) -> int: ...


@runtime_checkable
class CompetitorBrain(Protocol):
    """Minimum brain surface the benchmark adapters consume.

    Implementations should match the signatures exactly — the adapters
    call with keyword arguments. Return types match the Astrocyte types
    used in the rest of the eval pipeline so downstream code (result
    serialization, metric computation) doesn't branch on brain type.

    Adapters are free to raise ``NotImplementedError`` for parameters
    they don't support (e.g. ``occurred_at`` on a system that doesn't
    track timestamps) — the benchmark's outer loop then records the
    skip explicitly.
    """

    _pipeline: _PipelineLike | None

    async def retain(
        self,
        content: str,
        *,
        bank_id: str,
        metadata: dict | None = None,
        tags: list[str] | None = None,
        occurred_at: datetime | None = None,
        content_type: str = "text",
    ) -> RetainResult: ...

    async def recall(
        self,
        query: str,
        *,
        bank_id: str,
        max_results: int = 10,
    ) -> RecallResult: ...

    async def reflect(
        self,
        query: str,
        *,
        bank_id: str,
        max_tokens: int | None = None,
    ) -> ReflectResult: ...
