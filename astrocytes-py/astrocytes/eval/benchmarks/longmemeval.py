"""LongMemEval benchmark adapter.

LongMemEval (ICLR 2025) tests five long-term memory abilities:
1. Information extraction
2. Multi-session reasoning
3. Temporal reasoning
4. Knowledge updates
5. Abstention

Dataset: https://github.com/xiaowu0162/LongMemEval
Paper: https://arxiv.org/abs/2410.10813

Usage:
    from astrocytes.eval.benchmarks.longmemeval import LongMemEvalBenchmark

    bench = LongMemEvalBenchmark(brain)
    results = await bench.run(data_path="./LongMemEval/data", bank_id="bench-lme")
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrocytes.eval.metrics import text_overlap_score
from astrocytes.types import EvalMetrics, EvalResult, ForgetRequest, QueryResult

if TYPE_CHECKING:
    from astrocytes._astrocyte import Astrocyte


@dataclass
class LongMemEvalResult:
    """Results from a LongMemEval benchmark run."""

    overall_accuracy: float
    category_accuracy: dict[str, float]
    total_questions: int
    correct: int
    per_question: list[dict[str, Any]]
    eval_result: EvalResult  # Standard Astrocytes EvalResult


@dataclass
class LongMemEvalQuestion:
    """A single LongMemEval question."""

    question_id: str
    category: str  # "extraction", "reasoning", "temporal", "update", "abstention"
    question: str
    answer: str
    session_ids: list[str]
    conversation_context: list[dict[str, str]]  # Chat history


class LongMemEvalBenchmark:
    """Adapter for running LongMemEval against Astrocytes.

    Loads the LongMemEval dataset, retains conversation sessions,
    then evaluates recall/reflect against the 500 questions.
    """

    def __init__(self, brain: Astrocyte) -> None:
        self.brain = brain

    async def run(
        self,
        data_path: str | Path | None = None,
        questions: list[LongMemEvalQuestion] | None = None,
        bank_id: str = "bench-longmemeval",
        *,
        clean_after: bool = True,
        max_questions: int | None = None,
    ) -> LongMemEvalResult:
        """Run the LongMemEval benchmark.

        Args:
            data_path: Path to LongMemEval data directory (contains .json files).
                       If None, `questions` must be provided.
            questions: Pre-loaded questions (alternative to data_path).
            bank_id: Dedicated bank for the benchmark.
            clean_after: Delete the bank after running.
            max_questions: Limit number of questions (for quick testing).

        Returns:
            LongMemEvalResult with accuracy breakdown by category.
        """
        if questions is None and data_path is not None:
            questions = load_longmemeval_dataset(data_path, max_questions=max_questions)
        elif questions is None:
            raise ValueError("Either data_path or questions must be provided")

        if max_questions and len(questions) > max_questions:
            questions = questions[:max_questions]

        start_time = time.monotonic()
        retain_latencies: list[float] = []
        recall_latencies: list[float] = []

        # ── Phase 1: Retain conversation sessions ──
        sessions_retained: set[str] = set()
        for q in questions:
            for msg in q.conversation_context:
                session_key = f"{q.question_id}:{msg.get('session_id', '')}"
                if session_key in sessions_retained:
                    continue
                sessions_retained.add(session_key)

                content = msg.get("content", "")
                if not content.strip():
                    continue

                t0 = time.monotonic()
                await self.brain.retain(
                    content,
                    bank_id=bank_id,
                    tags=[msg.get("role", "user"), q.category],
                    metadata={"session_id": msg.get("session_id", ""), "source": "longmemeval"},
                )
                retain_latencies.append((time.monotonic() - t0) * 1000)

        # ── Phase 2: Evaluate questions ──
        correct = 0
        category_correct: dict[str, int] = {}
        category_total: dict[str, int] = {}
        per_question: list[dict[str, Any]] = []
        query_results: list[QueryResult] = []

        for q in questions:
            category_total[q.category] = category_total.get(q.category, 0) + 1

            t0 = time.monotonic()
            result = await self.brain.recall(q.question, bank_id=bank_id, max_results=10)
            elapsed = (time.monotonic() - t0) * 1000
            recall_latencies.append(elapsed)

            # Check if the expected answer appears in any recall hit
            answer_found = any(text_overlap_score([q.answer], h.text) > 0.3 for h in result.hits)

            # Also try reflect for a more thorough check
            reflect_result = await self.brain.reflect(q.question, bank_id=bank_id)
            answer_in_reflect = text_overlap_score([q.answer], reflect_result.answer) > 0.3

            is_correct = answer_found or answer_in_reflect
            if is_correct:
                correct += 1
                category_correct[q.category] = category_correct.get(q.category, 0) + 1

            per_question.append(
                {
                    "question_id": q.question_id,
                    "category": q.category,
                    "question": q.question,
                    "expected_answer": q.answer,
                    "correct": is_correct,
                    "recall_hits": len(result.hits),
                    "reflect_answer_preview": reflect_result.answer[:200],
                }
            )

            # Build QueryResult for standard metrics
            relevant_ids: set[str] = set()
            for h in result.hits:
                if h.memory_id and text_overlap_score([q.answer], h.text) > 0.3:
                    relevant_ids.add(h.memory_id)
            retrieved_ids = [h.memory_id or "" for h in result.hits]

            query_results.append(
                QueryResult(
                    query=q.question,
                    expected=[q.answer],
                    actual=result.hits,
                    relevant_found=len(relevant_ids),
                    precision=len(relevant_ids) / max(len(retrieved_ids), 1),
                    reciprocal_rank=next(
                        (1.0 / (i + 1) for i, rid in enumerate(retrieved_ids) if rid in relevant_ids),
                        0.0,
                    ),
                    latency_ms=elapsed,
                )
            )

        # ── Phase 3: Compute results ──
        total_duration = time.monotonic() - start_time
        overall_accuracy = correct / max(len(questions), 1)

        category_accuracy: dict[str, float] = {}
        for cat in category_total:
            category_accuracy[cat] = category_correct.get(cat, 0) / category_total[cat]

        from astrocytes.eval.metrics import percentile

        metrics = EvalMetrics(
            recall_precision=sum(qr.precision for qr in query_results) / max(len(query_results), 1),
            recall_hit_rate=sum(1.0 for qr in query_results if qr.relevant_found > 0) / max(len(query_results), 1),
            recall_mrr=sum(qr.reciprocal_rank for qr in query_results) / max(len(query_results), 1),
            recall_ndcg=0.0,  # Not computed for LongMemEval (binary correctness)
            retain_latency_p50_ms=percentile(retain_latencies, 50),
            retain_latency_p95_ms=percentile(retain_latencies, 95),
            recall_latency_p50_ms=percentile(recall_latencies, 50),
            recall_latency_p95_ms=percentile(recall_latencies, 95),
            total_tokens_used=0,
            total_duration_seconds=total_duration,
            reflect_accuracy=overall_accuracy,
        )

        eval_result = EvalResult(
            suite="longmemeval",
            provider=self.brain._provider_name,
            provider_tier=self.brain._config.provider_tier,
            timestamp=datetime.now(timezone.utc),
            metrics=metrics,
            per_query_results=query_results,
            config_snapshot={"benchmark": "longmemeval", "total_questions": len(questions)},
        )

        # ── Cleanup ──
        if clean_after:
            try:
                await self.brain._do_forget(ForgetRequest(bank_id=bank_id, scope="all"))
            except Exception:
                pass

        return LongMemEvalResult(
            overall_accuracy=overall_accuracy,
            category_accuracy=category_accuracy,
            total_questions=len(questions),
            correct=correct,
            per_question=per_question,
            eval_result=eval_result,
        )


def load_longmemeval_dataset(
    data_path: str | Path,
    max_questions: int | None = None,
) -> list[LongMemEvalQuestion]:
    """Load LongMemEval dataset from JSON files.

    Expects the directory structure from https://github.com/xiaowu0162/LongMemEval:
    - data_path/questions.json (or *.json files with question arrays)

    Returns list of LongMemEvalQuestion objects.
    """
    data_path = Path(data_path)
    questions: list[LongMemEvalQuestion] = []

    # Try loading from a single questions.json or all JSON files
    json_files = list(data_path.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"No JSON files found in {data_path}")

    for json_file in json_files:
        with open(json_file) as f:
            data = json.load(f)

        # Handle both single-object and array formats
        items = data if isinstance(data, list) else [data]

        for item in items:
            if not isinstance(item, dict):
                continue
            # Map to our question format — adapt field names from LongMemEval
            q = LongMemEvalQuestion(
                question_id=str(item.get("id", item.get("question_id", ""))),
                category=item.get("category", item.get("type", "general")),
                question=item.get("question", item.get("query", "")),
                answer=item.get("answer", item.get("gold_answer", "")),
                session_ids=item.get("session_ids", []),
                conversation_context=item.get("conversation", item.get("context", [])),
            )
            if q.question and q.answer:
                questions.append(q)

        if max_questions and len(questions) >= max_questions:
            break

    return questions[:max_questions] if max_questions else questions
