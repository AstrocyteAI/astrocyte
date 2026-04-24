"""Competitor memory-system adapters for head-to-head benchmarks.

Each adapter wraps a third-party memory system (Mem0, Zep) with a shim
that duck-types as an Astrocyte brain. That lets the existing
benchmark adapters run against them unchanged: every system in a
published comparison matrix goes through the **same** LoCoMo /
LongMemEval code path, the **same** canonical judge, and the
**same** reflect LLM — so the number reported for ``mem0`` is
produced by the exact machinery that produced the number for
``astrocyte``, just with a different brain underneath.

Scope today is deliberately narrow — scaffolding only. Real SDK calls
are stubbed with explicit errors pointing to what needs wiring. Tests
pin the duck-type contract so when Mem0 / Zep SDK wiring lands, the
adapters drop in without touching benchmark code.

See module docs:

- :mod:`astrocyte.eval.competitors.base` — :class:`CompetitorBrain`
  protocol + factory.
- :mod:`astrocyte.eval.competitors.mem0_brain` — Mem0 adapter stub.
- :mod:`astrocyte.eval.competitors.zep_brain` — Zep adapter stub.
"""

from astrocyte.eval.competitors.base import (
    CompetitorBrain,
    CompetitorNotConfigured,
    build_competitor_brain,
)

__all__ = [
    "CompetitorBrain",
    "CompetitorNotConfigured",
    "build_competitor_brain",
]
