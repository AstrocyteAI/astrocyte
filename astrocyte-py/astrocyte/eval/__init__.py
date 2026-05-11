"""Astrocyte evaluation utilities.

Used by the PageIndex bench scripts in ``scripts/bench_pageindex_*.py``.
Surface is intentionally minimal — just the LLM judges and the
terminal-error classifier. The v0.x bench harness
(``scripts/run_benchmarks.py``) and its supporting modules
(``MemoryEvaluator``, ``compare_providers``, etc.) were removed in May
2026; see ``docs/_design/benchmark-comparison-methodology.md``.
"""

from astrocyte.eval._terminal_error import is_terminal_error

__all__ = ["is_terminal_error"]
