"""Astrocytes evaluation harness — benchmark suites, provider comparison, regression detection.

Usage:
    from astrocyte.eval import MemoryEvaluator, compare_providers

    evaluator = MemoryEvaluator(brain)
    results = await evaluator.run_suite("basic", bank_id="eval-bank")
    print(results.summary())

See docs/_design/evaluation.md for the full specification.
"""

from astrocyte.eval.evaluator import MemoryEvaluator, compare_providers

__all__ = ["MemoryEvaluator", "compare_providers"]
