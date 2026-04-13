"""Memory evaluator — runs suites, computes metrics, detects regressions.

See docs/_design/evaluation.md for the full specification.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from astrocyte.eval.metrics import (
    ndcg_at_k,
    percentile,
    precision_at_k,
    reciprocal_rank,
    text_overlap_score,
)
from astrocyte.eval.suite import EvalSuite, load_suite
from astrocyte.types import (
    EvalMetrics,
    EvalResult,
    ForgetRequest,
    QueryResult,
    RegressionAlert,
)

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase helpers — each returns the data collected for that evaluation phase
# ---------------------------------------------------------------------------


async def _run_retain_phase(
    brain: Astrocyte,
    suite: EvalSuite,
    bank_id: str,
) -> tuple[dict[str, str], list[float]]:
    """Phase 1: Retain all test memories. Returns (memory_map, latencies)."""
    memory_map: dict[str, str] = {}
    latencies: list[float] = []

    for case in suite.retains:
        t0 = time.monotonic()
        result = await brain.retain(
            case.content,
            bank_id=bank_id,
            tags=case.tags,
        )
        latencies.append((time.monotonic() - t0) * 1000)
        if result.stored and result.memory_id:
            memory_map[case.content] = result.memory_id

    return memory_map, latencies


async def _run_recall_phase(
    brain: Astrocyte,
    suite: EvalSuite,
    bank_id: str,
) -> tuple[list[QueryResult], list[float]]:
    """Phase 2: Run recall queries. Returns (query_results, latencies)."""
    query_results: list[QueryResult] = []
    latencies: list[float] = []

    for case in suite.recalls:
        t0 = time.monotonic()
        result = await brain.recall(
            case.query,
            bank_id=bank_id,
            max_results=case.max_results,
            tags=case.tags,
        )
        elapsed = (time.monotonic() - t0) * 1000
        latencies.append(elapsed)

        relevant_ids = _find_relevant_ids(case.expected_contains, result.hits)
        retrieved_ids = [h.memory_id or "" for h in result.hits]

        query_results.append(
            QueryResult(
                query=case.query,
                expected=case.expected_contains,
                actual=result.hits,
                relevant_found=sum(1 for rid in retrieved_ids if rid in relevant_ids),
                precision=precision_at_k(relevant_ids, retrieved_ids),
                reciprocal_rank=reciprocal_rank(relevant_ids, retrieved_ids),
                latency_ms=elapsed,
            )
        )

    return query_results, latencies


def _find_relevant_ids(expected_keywords: list[str], hits: list) -> set[str]:
    """Identify which hits are relevant based on expected keyword overlap."""
    relevant: set[str] = set()
    for hit in hits:
        if text_overlap_score(expected_keywords, hit.text) > 0 and hit.memory_id:
            relevant.add(hit.memory_id)
    return relevant


async def _run_reflect_phase(
    brain: Astrocyte,
    suite: EvalSuite,
    bank_id: str,
) -> tuple[list[float], list[str], list[list[str]], list[float]]:
    """Phase 3: Run reflect queries. Returns (scores, answers, source_texts, latencies)."""
    scores: list[float] = []
    answers: list[str] = []
    source_texts: list[list[str]] = []
    latencies: list[float] = []

    for case in suite.reflects:
        t0 = time.monotonic()
        result = await brain.reflect(case.query, bank_id=bank_id)
        latencies.append((time.monotonic() - t0) * 1000)
        answers.append(result.answer)
        source_texts.append([s.text for s in (result.sources or [])])
        scores.append(text_overlap_score(case.expected_topics, result.answer))

    return scores, answers, source_texts, latencies


async def _run_deepeval_judge(
    suite: EvalSuite,
    query_results: list[QueryResult],
    reflect_answers: list[str],
    reflect_source_texts: list[list[str]],
    judge_model: str | None,
) -> object | None:
    """Phase 3b: Optional DeepEval LLM-judged scoring."""
    from astrocyte.eval.deepeval_judge import run_deepeval_judge

    recall_pairs = [
        {
            "query": qr.query,
            "retrieved_texts": [h.text for h in qr.actual],
            "expected": " ".join(qr.expected) if qr.expected else None,
        }
        for qr in query_results
    ]
    reflect_pairs = [
        {
            "query": case.query,
            "answer": reflect_answers[i] if i < len(reflect_answers) else "",
            "source_texts": reflect_source_texts[i] if i < len(reflect_source_texts) else [],
        }
        for i, case in enumerate(suite.reflects)
    ]
    return await run_deepeval_judge(
        recall_pairs=recall_pairs,
        reflect_pairs=reflect_pairs,
        model=judge_model,
    )


def _compute_metrics(
    query_results: list[QueryResult],
    retain_latencies: list[float],
    recall_latencies: list[float],
    reflect_scores: list[float],
    reflect_latencies: list[float],
    tokens_used: int,
    total_duration: float,
) -> EvalMetrics:
    """Phase 4: Compute aggregate metrics from per-query results."""
    precisions = [qr.precision for qr in query_results]
    hit_rates = [1.0 if qr.relevant_found > 0 else 0.0 for qr in query_results]
    mrrs = [qr.reciprocal_rank for qr in query_results]

    ndcgs: list[float] = []
    for qr in query_results:
        relevant_ids = _find_relevant_ids(qr.expected, qr.actual)
        retrieved = [h.memory_id or "" for h in qr.actual]
        ndcgs.append(ndcg_at_k(relevant_ids, retrieved))

    return EvalMetrics(
        recall_precision=_safe_mean(precisions),
        recall_hit_rate=_safe_mean(hit_rates),
        recall_mrr=_safe_mean(mrrs),
        recall_ndcg=_safe_mean(ndcgs),
        retain_latency_p50_ms=percentile(retain_latencies, 50),
        retain_latency_p95_ms=percentile(retain_latencies, 95),
        recall_latency_p50_ms=percentile(recall_latencies, 50),
        recall_latency_p95_ms=percentile(recall_latencies, 95),
        total_tokens_used=tokens_used,
        total_duration_seconds=total_duration,
        reflect_accuracy=_safe_mean(reflect_scores) if reflect_scores else None,
        reflect_latency_p50_ms=percentile(reflect_latencies, 50) if reflect_latencies else None,
        reflect_latency_p95_ms=percentile(reflect_latencies, 95) if reflect_latencies else None,
    )


# ---------------------------------------------------------------------------
# Main evaluator
# ---------------------------------------------------------------------------


class MemoryEvaluator:
    """Runs evaluation suites against an Astrocyte instance.

    Usage:
        evaluator = MemoryEvaluator(brain)
        results = await evaluator.run_suite("basic", bank_id="eval-bank")
        print(results.summary())
    """

    def __init__(self, brain: Astrocyte) -> None:
        self.brain = brain

    async def run_suite(
        self,
        suite: str | EvalSuite,
        bank_id: str = "eval-test",
        *,
        clean_after: bool = True,
        judge: str = "keyword",
        judge_model: str | None = None,
    ) -> EvalResult:
        """Run an evaluation suite and return results.

        Args:
            suite: Suite name ("basic", "accuracy") or EvalSuite instance or YAML path.
            bank_id: Dedicated bank for evaluation (created fresh).
            clean_after: Delete the eval bank after running.
            judge: Scoring method — "keyword" (fast, free) or "deepeval" (LLM-judged, thorough).
            judge_model: LLM model for DeepEval judge (e.g., "gpt-4o"). Uses DeepEval default if None.
        """
        if isinstance(suite, str):
            eval_suite = load_suite(suite)
        else:
            eval_suite = suite

        start_time = time.monotonic()

        # Reset the pipeline's token counter so we measure only this run
        if self.brain._pipeline:
            self.brain._pipeline.reset_token_counter()

        # Phase 1–3: retain, recall, reflect
        _memory_map, retain_latencies = await _run_retain_phase(self.brain, eval_suite, bank_id)
        query_results, recall_latencies = await _run_recall_phase(self.brain, eval_suite, bank_id)
        reflect_scores, reflect_answers, reflect_source_texts, reflect_latencies = await _run_reflect_phase(
            self.brain, eval_suite, bank_id
        )

        # Phase 3b: optional DeepEval judge
        deepeval_scores = None
        if judge == "deepeval":
            deepeval_scores = await _run_deepeval_judge(
                eval_suite, query_results, reflect_answers, reflect_source_texts, judge_model
            )

        # Phase 4: aggregate metrics
        metrics = _compute_metrics(
            query_results=query_results,
            retain_latencies=retain_latencies,
            recall_latencies=recall_latencies,
            reflect_scores=reflect_scores,
            reflect_latencies=reflect_latencies,
            tokens_used=self.brain._pipeline.tokens_used if self.brain._pipeline else 0,
            total_duration=time.monotonic() - start_time,
        )

        # Phase 5: cleanup
        if clean_after:
            try:
                await self.brain._do_forget(ForgetRequest(bank_id=bank_id, scope="all"))
            except Exception:
                logger.debug("eval cleanup forget failed (best-effort)", exc_info=True)

        # Build result with optional DeepEval scores
        config_snapshot: dict[str, str | int | float | bool | None] = {"judge": judge}
        if judge == "deepeval" and deepeval_scores:
            config_snapshot["deepeval_contextual_relevancy"] = deepeval_scores.contextual_relevancy
            config_snapshot["deepeval_faithfulness"] = deepeval_scores.faithfulness
            config_snapshot["deepeval_answer_relevancy"] = deepeval_scores.answer_relevancy
            config_snapshot["deepeval_hallucination"] = deepeval_scores.hallucination

        return EvalResult(
            suite=eval_suite.name,
            provider=self.brain._provider_name,
            provider_tier=self.brain._config.provider_tier,
            timestamp=datetime.now(timezone.utc),
            metrics=metrics,
            per_query_results=query_results,
            config_snapshot=config_snapshot,
        )


def detect_regressions(
    current: EvalMetrics,
    baseline: EvalMetrics,
    threshold: float = 0.05,
) -> list[RegressionAlert]:
    """Compare current metrics against a baseline. Returns alerts for regressions.

    A regression is when a metric drops by more than `threshold` (as a fraction).
    """
    alerts: list[RegressionAlert] = []

    checks = [
        ("recall_precision", current.recall_precision, baseline.recall_precision),
        ("recall_hit_rate", current.recall_hit_rate, baseline.recall_hit_rate),
        ("recall_mrr", current.recall_mrr, baseline.recall_mrr),
        ("recall_ndcg", current.recall_ndcg, baseline.recall_ndcg),
    ]

    if current.reflect_accuracy is not None and baseline.reflect_accuracy is not None:
        checks.append(("reflect_accuracy", current.reflect_accuracy, baseline.reflect_accuracy))

    for metric_name, current_val, baseline_val in checks:
        if baseline_val == 0:
            continue
        delta = current_val - baseline_val
        delta_pct = delta / baseline_val

        if delta_pct < -threshold:
            severity = "critical" if delta_pct < -2 * threshold else "warning"
            alerts.append(
                RegressionAlert(
                    metric=metric_name,
                    current_value=current_val,
                    baseline_value=baseline_val,
                    delta=delta,
                    delta_percent=delta_pct,
                    severity=severity,
                )
            )

    return alerts


async def compare_providers(
    configs: list[str],
    suite: str = "basic",
    bank_id: str = "eval-compare",
) -> list[EvalResult]:
    """Run the same suite against multiple provider configs and return results.

    Args:
        configs: List of YAML config file paths.
        suite: Suite name or path.
        bank_id: Base bank ID (suffixed with provider name).

    Returns:
        List of EvalResult, one per config.
    """
    from astrocyte._astrocyte import Astrocyte

    results: list[EvalResult] = []
    for config_path in configs:
        brain = Astrocyte.from_config(config_path)
        evaluator = MemoryEvaluator(brain)
        provider_bank = f"{bank_id}-{brain._provider_name}"
        result = await evaluator.run_suite(suite, bank_id=provider_bank, clean_after=True)
        results.append(result)

    return results


def format_comparison(results: list[EvalResult]) -> str:
    """Format a side-by-side comparison table."""
    if not results:
        return "No results to compare."

    lines: list[str] = []
    header = f"{'Metric':<25}" + "".join(f"{r.provider:>20}" for r in results)
    lines.append(header)
    lines.append("─" * len(header))

    def row(label: str, values: list[float | None], fmt: str = ".4f") -> str:
        cells = f"{label:<25}"
        for v in values:
            cells += f"{v:>20{fmt}}" if v is not None else f"{'N/A':>20}"
        return cells

    m = [r.metrics for r in results]
    lines.append(row("Recall precision", [x.recall_precision for x in m]))
    lines.append(row("Recall hit rate", [x.recall_hit_rate for x in m]))
    lines.append(row("Recall MRR", [x.recall_mrr for x in m]))
    lines.append(row("Recall NDCG", [x.recall_ndcg for x in m]))
    lines.append(row("Retain p50 (ms)", [x.retain_latency_p50_ms for x in m], ".1f"))
    lines.append(row("Retain p95 (ms)", [x.retain_latency_p95_ms for x in m], ".1f"))
    lines.append(row("Recall p50 (ms)", [x.recall_latency_p50_ms for x in m], ".1f"))
    lines.append(row("Recall p95 (ms)", [x.recall_latency_p95_ms for x in m], ".1f"))
    lines.append(row("Reflect accuracy", [x.reflect_accuracy for x in m]))
    lines.append(row("Duration (s)", [x.total_duration_seconds for x in m], ".2f"))

    return "\n".join(lines)


def _safe_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)
