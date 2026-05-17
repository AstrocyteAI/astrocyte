"""LLM-as-judge scoring for FinanceBench (Mafin2.5 parity)."""

from __future__ import annotations

import logging
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# async def llm_call(system: str, user: str) -> str
LlmCall = Callable[[str, str], Awaitable[str]]


async def judge(
    question: str,
    ground_truth: str,
    model_answer: str,
    llm_call: LlmCall,
) -> bool:
    """Return True if model_answer is correct per LLM judge.

    Degrades gracefully on LLM failure — marks the question incorrect
    and logs a warning rather than crashing the run.
    """
    from scripts.financebench._prompts import JUDGE_SYSTEM, JUDGE_USER

    user_msg = JUDGE_USER.format(
        question=question,
        ground_truth=ground_truth,
        model_answer=model_answer,
    )
    try:
        verdict = await llm_call(JUDGE_SYSTEM, user_msg)
        return verdict.strip().upper().startswith("CORRECT")
    except Exception as exc:  # noqa: BLE001
        logger.warning("judge LLM call failed — marking incorrect: %s", exc)
        return False
