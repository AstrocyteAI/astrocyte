"""Canonical benchmark judges — ported from published eval scripts.

The previous Astrocyte benchmark adapters used ``word_overlap_score > 0.3``
as a coarse proxy for correctness. That's looser than what published
comparison points (LoCoMo paper, LongMemEval paper, Mem0, Zep, Hindsight)
use — so our numbers could not be directly compared.

This package ports each benchmark's canonical judge exactly so our
scores become cross-comparable with published work:

- :mod:`astrocyte.eval.judges.locomo_judge` — stemmed token-F1 with
  category-specific logic. Pure Python, no LLM. Ported from
  ``datasets/locomo/task_eval/evaluation.py``.

- :mod:`astrocyte.eval.judges.longmemeval_judge` — LLM-judge with
  task-specific yes/no prompts. Ported from
  ``datasets/longmemeval/src/evaluation/evaluate_qa.py``.

Each judge is self-contained; the adapter selects which judge to use
based on the benchmark it's running. Adapters can also be configured to
run BOTH their legacy scorer and the canonical judge for delta analysis.
"""

from astrocyte.eval.judges.locomo_judge import (
    LOCOMO_CATEGORY_IDS,
    locomo_category_id,
    locomo_score_qa,
)
from astrocyte.eval.judges.longmemeval_judge import (
    LONGMEMEVAL_ABSTENTION_SUFFIX,
    LongMemEvalJudge,
    build_longmemeval_judge_prompt,
)

__all__ = [
    "LOCOMO_CATEGORY_IDS",
    "LONGMEMEVAL_ABSTENTION_SUFFIX",
    "LongMemEvalJudge",
    "build_longmemeval_judge_prompt",
    "locomo_category_id",
    "locomo_score_qa",
]
