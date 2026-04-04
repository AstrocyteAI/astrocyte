"""DeepEval LLM-as-judge integration for Astrocyte evaluation.

Provides thorough, LLM-judged evaluation using DeepEval's RAG metrics:
- ContextualRelevancy: are the recalled memories relevant to the query?
- ContextualPrecision: are the top-ranked results the most relevant?
- Faithfulness: is the reflect synthesis grounded in the recalled memories?
- AnswerRelevancy: does the synthesis actually answer the question?
- HallucinationMetric: did reflect fabricate information not in memory?

Requires: pip install deepeval (or pip install astrocyte[eval])

Usage:
    from astrocyte.eval import MemoryEvaluator

    evaluator = MemoryEvaluator(brain)
    results = await evaluator.run_suite("basic", bank_id="eval", judge="deepeval")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeepEvalScores:
    """Aggregated DeepEval metric scores for an evaluation run."""

    contextual_relevancy: float | None = None
    contextual_precision: float | None = None
    contextual_recall: float | None = None
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    hallucination: float | None = None
    per_query_scores: list[dict[str, Any]] = field(default_factory=list)


async def score_recall_with_deepeval(
    query: str,
    retrieved_texts: list[str],
    expected_output: str | None = None,
    *,
    model: str | None = None,
    threshold: float = 0.5,
) -> dict[str, float | None]:
    """Score a single recall result using DeepEval RAG metrics.

    Args:
        query: The recall query.
        retrieved_texts: List of texts from recall hits.
        expected_output: Optional expected answer (improves precision/recall scoring).
        model: LLM model for the judge (e.g., "gpt-4o"). Uses DeepEval default if None.
        threshold: Score threshold for pass/fail.

    Returns:
        Dict of metric_name → score (0.0–1.0).
    """
    try:
        from deepeval.metrics import (
            ContextualPrecisionMetric,
            ContextualRelevancyMetric,
        )
        from deepeval.test_case import LLMTestCase
    except ImportError:
        raise ImportError(
            "DeepEval is required for LLM-judged evaluation. "
            "Install it with: pip install deepeval (or pip install astrocyte[eval])"
        )

    # DeepEval needs actual_output — use concatenated retrieved texts if no expected
    actual_output = expected_output or " ".join(retrieved_texts[:3]) or "No results found."

    test_case = LLMTestCase(
        input=query,
        actual_output=actual_output,
        retrieval_context=retrieved_texts or ["No context retrieved."],
        expected_output=expected_output,
    )

    scores: dict[str, float | None] = {}

    # Contextual Relevancy — are retrieved memories relevant to the query?
    metric_kwargs: dict[str, Any] = {"threshold": threshold}
    if model:
        metric_kwargs["model"] = model

    try:
        relevancy = ContextualRelevancyMetric(**metric_kwargs)
        await relevancy.a_measure(test_case)
        scores["contextual_relevancy"] = relevancy.score
    except Exception:
        scores["contextual_relevancy"] = None

    # Contextual Precision — are the top-ranked results the most relevant?
    if expected_output:
        try:
            precision = ContextualPrecisionMetric(**metric_kwargs)
            await precision.a_measure(test_case)
            scores["contextual_precision"] = precision.score
        except Exception:
            scores["contextual_precision"] = None

    return scores


async def score_reflect_with_deepeval(
    query: str,
    answer: str,
    source_texts: list[str],
    *,
    model: str | None = None,
    threshold: float = 0.5,
) -> dict[str, float | None]:
    """Score a single reflect result using DeepEval metrics.

    Args:
        query: The reflect query.
        answer: The synthesized answer from reflect.
        source_texts: Texts from the source memories used for synthesis.
        model: LLM model for the judge.
        threshold: Score threshold.

    Returns:
        Dict of metric_name → score (0.0–1.0).
    """
    try:
        from deepeval.metrics import (
            AnswerRelevancyMetric,
            FaithfulnessMetric,
            HallucinationMetric,
        )
        from deepeval.test_case import LLMTestCase
    except ImportError:
        raise ImportError(
            "DeepEval is required for LLM-judged evaluation. "
            "Install it with: pip install deepeval (or pip install astrocyte[eval])"
        )

    test_case = LLMTestCase(
        input=query,
        actual_output=answer,
        retrieval_context=source_texts or ["No context available."],
    )

    scores: dict[str, float | None] = {}
    metric_kwargs: dict[str, Any] = {"threshold": threshold}
    if model:
        metric_kwargs["model"] = model

    # Faithfulness — is the answer grounded in the recalled memories?
    try:
        faithfulness = FaithfulnessMetric(**metric_kwargs)
        await faithfulness.a_measure(test_case)
        scores["faithfulness"] = faithfulness.score
    except Exception:
        scores["faithfulness"] = None

    # Answer Relevancy — does the answer address the question?
    try:
        relevancy = AnswerRelevancyMetric(**metric_kwargs)
        await relevancy.a_measure(test_case)
        scores["answer_relevancy"] = relevancy.score
    except Exception:
        scores["answer_relevancy"] = None

    # Hallucination — did the answer fabricate information?
    try:
        hallucination = HallucinationMetric(**metric_kwargs)
        test_case_for_hallucination = LLMTestCase(
            input=query,
            actual_output=answer,
            context=source_texts or ["No context available."],
        )
        await hallucination.a_measure(test_case_for_hallucination)
        scores["hallucination"] = hallucination.score
    except Exception:
        scores["hallucination"] = None

    return scores


async def run_deepeval_judge(
    recall_pairs: list[dict[str, Any]],
    reflect_pairs: list[dict[str, Any]],
    *,
    model: str | None = None,
    threshold: float = 0.5,
) -> DeepEvalScores:
    """Run full DeepEval evaluation across all recall and reflect results.

    Args:
        recall_pairs: List of {"query": str, "retrieved_texts": list[str], "expected": str|None}
        reflect_pairs: List of {"query": str, "answer": str, "source_texts": list[str]}
        model: LLM judge model.
        threshold: Score threshold.

    Returns:
        Aggregated DeepEvalScores.
    """
    all_relevancy: list[float] = []
    all_precision: list[float] = []
    all_faithfulness: list[float] = []
    all_answer_relevancy: list[float] = []
    all_hallucination: list[float] = []
    per_query: list[dict[str, Any]] = []

    # Score recall queries
    for pair in recall_pairs:
        scores = await score_recall_with_deepeval(
            query=pair["query"],
            retrieved_texts=pair["retrieved_texts"],
            expected_output=pair.get("expected"),
            model=model,
            threshold=threshold,
        )
        if scores.get("contextual_relevancy") is not None:
            all_relevancy.append(scores["contextual_relevancy"])
        if scores.get("contextual_precision") is not None:
            all_precision.append(scores["contextual_precision"])
        per_query.append({"type": "recall", "query": pair["query"], **scores})

    # Score reflect queries
    for pair in reflect_pairs:
        scores = await score_reflect_with_deepeval(
            query=pair["query"],
            answer=pair["answer"],
            source_texts=pair["source_texts"],
            model=model,
            threshold=threshold,
        )
        if scores.get("faithfulness") is not None:
            all_faithfulness.append(scores["faithfulness"])
        if scores.get("answer_relevancy") is not None:
            all_answer_relevancy.append(scores["answer_relevancy"])
        if scores.get("hallucination") is not None:
            all_hallucination.append(scores["hallucination"])
        per_query.append({"type": "reflect", "query": pair["query"], **scores})

    return DeepEvalScores(
        contextual_relevancy=_safe_mean(all_relevancy),
        contextual_precision=_safe_mean(all_precision),
        faithfulness=_safe_mean(all_faithfulness),
        answer_relevancy=_safe_mean(all_answer_relevancy),
        hallucination=_safe_mean(all_hallucination),
        per_query_scores=per_query,
    )


def _safe_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)
