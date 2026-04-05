"""LoCoMo benchmark adapter.

LoCoMo (ECAI 2025) tests very long-term conversational memory across
four QA categories:
1. Single-hop — direct fact recall
2. Multi-hop — reasoning across multiple facts
3. Open-domain — broad knowledge questions
4. Temporal — time-aware reasoning

Dataset: https://github.com/snap-research/locomo
Paper: https://arxiv.org/abs/2402.17753

Usage:
    from astrocyte.eval.benchmarks.locomo import LoComoBenchmark

    bench = LoComoBenchmark(brain)
    results = await bench.run(data_path="./locomo/data/locomo10.json", bank_id="bench-locomo")
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrocyte.eval.metrics import text_overlap_score
from astrocyte.types import EvalMetrics, EvalResult, ForgetRequest, QueryResult

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


@dataclass
class LoCoMoResult:
    """Results from a LoCoMo benchmark run."""

    overall_accuracy: float
    category_accuracy: dict[str, float]
    total_questions: int
    correct: int
    per_question: list[dict[str, Any]]
    eval_result: EvalResult


@dataclass
class LoCoMoQuestion:
    """A single LoCoMo QA entry."""

    question: str
    answer: str
    category: str  # "single-hop", "multi-hop", "open-domain", "temporal"
    evidence_ids: list[str]
    conversation_id: str


@dataclass
class LoCoMoConversation:
    """A LoCoMo conversation with sessions."""

    conversation_id: str
    sessions: list[LoCoMoSession]
    questions: list[LoCoMoQuestion]


@dataclass
class LoCoMoSession:
    """A single session within a LoCoMo conversation."""

    session_id: str
    turns: list[dict[str, str]]  # [{"speaker": "...", "text": "..."}]
    date_time: str | None = None


class LoComoBenchmark:
    """Adapter for running LoCoMo against Astrocyte.

    Loads the LoCoMo dataset, retains conversation sessions,
    then evaluates recall/reflect against the QA questions.
    """

    def __init__(self, brain: Astrocyte) -> None:
        self.brain = brain

    async def run(
        self,
        data_path: str | Path | None = None,
        conversations: list[LoCoMoConversation] | None = None,
        bank_id: str = "bench-locomo",
        *,
        clean_after: bool = True,
        max_questions: int | None = None,
    ) -> LoCoMoResult:
        """Run the LoCoMo benchmark.

        Args:
            data_path: Path to locomo10.json (or directory containing it).
            conversations: Pre-loaded conversations (alternative to data_path).
            bank_id: Dedicated bank for the benchmark.
            clean_after: Delete the bank after running.
            max_questions: Limit number of questions.

        Returns:
            LoCoMoResult with accuracy breakdown by category.
        """
        if conversations is None and data_path is not None:
            conversations = load_locomo_dataset(data_path)
        elif conversations is None:
            raise ValueError("Either data_path or conversations must be provided")

        start_time = time.monotonic()
        retain_latencies: list[float] = []
        recall_latencies: list[float] = []

        # ── Phase 1: Retain all conversation sessions ──
        for convo in conversations:
            for session in convo.sessions:
                # Combine all turns in the session into one memory
                session_text_parts = []
                for turn in session.turns:
                    speaker = turn.get("speaker", "unknown")
                    text = turn.get("text", "")
                    if text.strip():
                        session_text_parts.append(f"{speaker}: {text}")

                if not session_text_parts:
                    continue

                session_text = "\n".join(session_text_parts)
                t0 = time.monotonic()
                await self.brain.retain(
                    session_text,
                    bank_id=bank_id,
                    tags=["locomo", f"convo:{convo.conversation_id}", f"session:{session.session_id}"],
                    metadata={
                        "source": "locomo",
                        "conversation_id": convo.conversation_id,
                        "session_id": session.session_id,
                        "date_time": session.date_time or "",
                    },
                )
                retain_latencies.append((time.monotonic() - t0) * 1000)

        # ── Phase 2: Collect all questions ──
        all_questions: list[LoCoMoQuestion] = []
        for convo in conversations:
            all_questions.extend(convo.questions)

        if max_questions and len(all_questions) > max_questions:
            all_questions = all_questions[:max_questions]

        # ── Phase 3: Evaluate questions ──
        correct = 0
        category_correct: dict[str, int] = {}
        category_total: dict[str, int] = {}
        per_question: list[dict[str, Any]] = []
        query_results: list[QueryResult] = []

        for q in all_questions:
            category_total[q.category] = category_total.get(q.category, 0) + 1

            t0 = time.monotonic()
            result = await self.brain.recall(q.question, bank_id=bank_id, max_results=10)
            elapsed = (time.monotonic() - t0) * 1000
            recall_latencies.append(elapsed)

            # Check if answer appears in recall hits
            answer_in_recall = any(text_overlap_score([q.answer], h.text) > 0.3 for h in result.hits)

            # Also try reflect
            reflect_result = await self.brain.reflect(q.question, bank_id=bank_id)
            answer_in_reflect = text_overlap_score([q.answer], reflect_result.answer) > 0.3

            is_correct = answer_in_recall or answer_in_reflect
            if is_correct:
                correct += 1
                category_correct[q.category] = category_correct.get(q.category, 0) + 1

            per_question.append(
                {
                    "question": q.question,
                    "expected_answer": q.answer,
                    "category": q.category,
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

        # ── Phase 4: Compute results ──
        total_duration = time.monotonic() - start_time
        overall_accuracy = correct / max(len(all_questions), 1)

        category_accuracy: dict[str, float] = {}
        for cat in category_total:
            category_accuracy[cat] = category_correct.get(cat, 0) / category_total[cat]

        from astrocyte.eval.metrics import percentile

        metrics = EvalMetrics(
            recall_precision=sum(qr.precision for qr in query_results) / max(len(query_results), 1),
            recall_hit_rate=sum(1.0 for qr in query_results if qr.relevant_found > 0) / max(len(query_results), 1),
            recall_mrr=sum(qr.reciprocal_rank for qr in query_results) / max(len(query_results), 1),
            recall_ndcg=0.0,
            retain_latency_p50_ms=percentile(retain_latencies, 50),
            retain_latency_p95_ms=percentile(retain_latencies, 95),
            recall_latency_p50_ms=percentile(recall_latencies, 50),
            recall_latency_p95_ms=percentile(recall_latencies, 95),
            total_tokens_used=0,
            total_duration_seconds=total_duration,
            reflect_accuracy=overall_accuracy,
        )

        eval_result = EvalResult(
            suite="locomo",
            provider=self.brain._provider_name,
            provider_tier=self.brain._config.provider_tier,
            timestamp=datetime.now(timezone.utc),
            metrics=metrics,
            per_query_results=query_results,
            config_snapshot={"benchmark": "locomo", "total_questions": len(all_questions)},
        )

        # ── Cleanup ──
        if clean_after:
            try:
                await self.brain._do_forget(ForgetRequest(bank_id=bank_id, scope="all"))
            except Exception:
                pass

        return LoCoMoResult(
            overall_accuracy=overall_accuracy,
            category_accuracy=category_accuracy,
            total_questions=len(all_questions),
            correct=correct,
            per_question=per_question,
            eval_result=eval_result,
        )


def load_locomo_dataset(
    data_path: str | Path,
    max_conversations: int | None = None,
) -> list[LoCoMoConversation]:
    """Load LoCoMo dataset from JSON.

    Handles both:
    - Single file: locomo10.json (array of conversations)
    - Directory: looks for locomo10.json or *.json inside

    Returns list of LoCoMoConversation objects.
    """
    data_path = Path(data_path)

    if data_path.is_dir():
        candidates = list(data_path.glob("locomo*.json")) or list(data_path.glob("*.json"))
        if not candidates:
            raise FileNotFoundError(f"No JSON files found in {data_path}")
        data_path = candidates[0]

    with open(data_path) as f:
        raw = json.load(f)

    # Handle both array and single-object formats
    items = raw if isinstance(raw, list) else [raw]

    conversations: list[LoCoMoConversation] = []

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        convo_id = str(item.get("id", item.get("conversation_id", f"convo-{i}")))

        # Parse sessions
        sessions: list[LoCoMoSession] = []
        for key, value in item.items():
            if key.startswith("session_") and not key.endswith(("_observation", "_date_time", "_summary")):
                # This is a session with turns
                session_id = key
                date_key = f"{key}_date_time"
                date_time = item.get(date_key)

                turns: list[dict[str, str]] = []
                if isinstance(value, list):
                    for turn in value:
                        if isinstance(turn, dict):
                            turns.append(
                                {
                                    "speaker": turn.get("speaker", turn.get("name", "unknown")),
                                    "text": turn.get("text", turn.get("content", "")),
                                }
                            )

                sessions.append(
                    LoCoMoSession(
                        session_id=session_id,
                        turns=turns,
                        date_time=str(date_time) if date_time else None,
                    )
                )

        # Parse QA
        questions: list[LoCoMoQuestion] = []
        qa_data = item.get("qa", item.get("questions", []))
        if isinstance(qa_data, list):
            for qa in qa_data:
                if not isinstance(qa, dict) or "question" not in qa:
                    continue
                questions.append(
                    LoCoMoQuestion(
                        question=qa["question"],
                        answer=qa.get("answer", ""),
                        category=qa.get("category", "general"),
                        evidence_ids=qa.get("evidence", []),
                        conversation_id=convo_id,
                    )
                )

        if sessions or questions:
            conversations.append(
                LoCoMoConversation(
                    conversation_id=convo_id,
                    sessions=sessions,
                    questions=questions,
                )
            )

        if max_conversations and len(conversations) >= max_conversations:
            break

    return conversations
