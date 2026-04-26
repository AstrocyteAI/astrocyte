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

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrocyte.eval.checkpoint import BenchmarkCheckpoint
from astrocyte.eval.metrics import ndcg_at_k, word_overlap_score
from astrocyte.eval.rate_limiter import EvalRateLimiter
from astrocyte.types import EvalMetrics, EvalResult, ForgetRequest, QueryResult

# Minimum text overlap score to consider an answer correct.
# Tuned empirically: 0.3 balances recall (catching paraphrases) against
# precision (rejecting unrelated text). See eval/metrics.py for scoring details.
ANSWER_OVERLAP_THRESHOLD = 0.3

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
    #: Raw F1 mean across all questions. Populated only when the run
    #: used ``use_canonical_judge=True``; None otherwise. THIS is the
    #: paper's headline metric — ``overall_accuracy`` above is the
    #: pass/fail-at-0.3 rate Astrocyte reports for its own tracking.
    #: Cross-competitor comparisons should use ``canonical_f1_overall``.
    canonical_f1_overall: float | None = None
    #: Per-category F1 means. Empty dict under legacy scorer.
    canonical_f1_by_category: dict[str, float] = field(default_factory=dict)


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
        use_canonical_judge: bool = False,
        llm_judge: object | None = None,
        checkpoint: BenchmarkCheckpoint | None = None,
        eval_concurrency: int = 5,
        eval_rpm: int = 500,
        eval_tpm: int = 200_000,
    ) -> LoCoMoResult:
        """Run the LoCoMo benchmark.

        Args:
            data_path: Path to locomo10.json (or directory containing it).
            conversations: Pre-loaded conversations (alternative to data_path).
            bank_id: Dedicated bank for the benchmark.
            clean_after: Delete the bank after running.
            max_questions: Limit number of questions.
            use_canonical_judge: When True without ``llm_judge``, score with
                the canonical stemmed-token-F1 judge — the original paper's
                metric. When True with ``llm_judge``, the LLM judge drives
                accuracy; F1 means are not computed. When False (default),
                uses the legacy ``word_overlap_score > 0.3`` scorer.
            llm_judge: Optional :class:`~astrocyte.eval.judges.locomo_judge.LoCoMoLLMJudge`
                instance. When provided, accuracy is determined by LLM yes/no
                judgment, matching the convention used by Mem0 (ECAI 2025),
                Hindsight, and MemMachine. This is required for numbers
                directly comparable to published competitor scores.
            checkpoint: Optional :class:`~astrocyte.eval.checkpoint.BenchmarkCheckpoint`
                from a previous interrupted run. Already-retained sessions are
                skipped and already-evaluated questions use their cached score.
            eval_concurrency: Maximum number of questions evaluated in parallel.
                Set to 1 for fully serial behaviour identical to the original loop.
            eval_rpm: Requests-per-minute budget for the eval rate limiter.
            eval_tpm: Tokens-per-minute budget for the eval rate limiter.

        Returns:
            LoCoMoResult with accuracy breakdown by category.
        """
        if checkpoint is not None:
            print(f"  [LoCoMo] {checkpoint.resume_summary()}")

        if conversations is None and data_path is not None:
            conversations = load_locomo_dataset(data_path)
        elif conversations is None:
            raise ValueError("Either data_path or conversations must be provided")

        start_time = time.monotonic()
        retain_latencies: list[float] = []
        recall_latencies: list[float] = []

        # Reset token counter for this benchmark run
        if self.brain._pipeline:
            self.brain._pipeline.reset_token_counter()

        # ── Phase 1: Retain all conversation sessions ──
        total_sessions = sum(len(c.sessions) for c in conversations)
        print(f"  [LoCoMo] Retaining {total_sessions} sessions from {len(conversations)} conversations...")
        retain_count = 0
        date_parse_attempts = 0
        unparseable_date_count = 0
        retain_phase_start = time.monotonic()
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

                session_key = f"{convo.conversation_id}:{session.session_id}"
                if checkpoint is not None and checkpoint.is_session_retained(session_key):
                    continue

                session_text = "\n".join(session_text_parts)

                # Parse session date for temporal retrieval
                occurred_at = None
                if session.date_time:
                    date_parse_attempts += 1
                    date_formats = (
                        "%I:%M %p on %d %B, %Y",  # "10:04 am on 19 December, 2023" (LoCoMo native)
                        "%B %d, %Y",               # "January 15, 2026"
                        "%Y-%m-%d",                # "2026-01-15"
                        "%m/%d/%Y",                # "01/15/2026"
                        "%d %B %Y",                # "15 January 2026"
                    )
                    parsed = False
                    for fmt in date_formats:
                        try:
                            occurred_at = datetime.strptime(session.date_time, fmt).replace(tzinfo=timezone.utc)
                            parsed = True
                            break
                        except ValueError:
                            continue
                    if not parsed:
                        unparseable_date_count += 1
                        logging.getLogger("astrocyte.eval").debug(
                            "LoCoMo: failed to parse session date_time '%s' for "
                            "conversation_id=%s session_id=%s; supported formats=%s",
                            session.date_time,
                            convo.conversation_id,
                            session.session_id,
                            date_formats,
                        )

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
                    occurred_at=occurred_at,
                    content_type="conversation",
                )
                retain_latencies.append((time.monotonic() - t0) * 1000)
                retain_count += 1
                if checkpoint is not None:
                    checkpoint.record_session(session_key)
                if retain_count % 20 == 0:
                    elapsed_r = time.monotonic() - retain_phase_start
                    rate = retain_count / elapsed_r
                    remaining = (total_sessions - retain_count) / rate if rate > 0 else 0
                    print(
                        f"  [LoCoMo] Retained {retain_count}/{total_sessions} sessions "
                        f"({elapsed_r:.0f}s elapsed, ~{remaining:.0f}s remaining)",
                        flush=True,
                    )

        print(f"  [LoCoMo] Retain complete: {retain_count} sessions stored.")
        if unparseable_date_count > 0:
            logging.getLogger("astrocyte.eval").warning(
                "LoCoMo: %d/%d session date_time values were unparseable during retain phase. "
                "Consider extending supported date formats.",
                unparseable_date_count,
                date_parse_attempts,
            )

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
        # Canonical path: track raw F1 means per category so we can
        # report the numbers the paper actually publishes alongside the
        # pass/fail accuracy. Paper's headline numbers ARE F1 means,
        # not pass-rates — conflating the two underreports our scores by
        # dropping all partial-credit cases.
        category_f1_sum: dict[str, float] = {}
        total_f1_sum = 0.0
        ndcg_sum = 0.0
        completed_count = 0

        total_q = len(all_questions)
        # Pre-allocate per-index slots so concurrent workers can write
        # results in any order without coordination.
        per_question_arr: list[dict[str, Any] | None] = [None] * total_q
        query_results_arr: list[QueryResult | None] = [None] * total_q

        rate_limiter = EvalRateLimiter(
            max_concurrency=eval_concurrency,
            rpm=eval_rpm,
            tpm=eval_tpm,
        )

        print(f"  [LoCoMo] Evaluating {total_q} questions (concurrency={eval_concurrency})...")
        eval_phase_start = time.monotonic()

        def _progress_print() -> None:
            """Print progress line. Must be called after incrementing completed_count."""
            n = completed_count
            if n == 1 or n % 10 == 0 or n == total_q:
                acc_so_far = correct / n if n > 0 else 0.0
                elapsed_e = time.monotonic() - eval_phase_start
                rate_e = n / elapsed_e if elapsed_e > 0 else 0
                remaining_e = (total_q - n) / rate_e if rate_e > 0 else 0
                print(
                    f"  [LoCoMo] Question {n}/{total_q} — "
                    f"accuracy: {acc_so_far:.1%} ({correct}/{n}) — "
                    f"~{remaining_e:.0f}s remaining",
                    flush=True,
                )

        async def _eval_one(idx: int, q: LoCoMoQuestion) -> None:
            nonlocal correct, total_f1_sum, ndcg_sum, completed_count

            category_total[q.category] = category_total.get(q.category, 0) + 1
            q_key = f"{q.conversation_id}:{idx}"

            # Restore cached result from a previous interrupted run
            # (no rate limiter needed — no LLM calls).
            if checkpoint is not None and checkpoint.is_question_evaluated(q_key):
                cached = checkpoint.get_question_result(q_key)
                is_correct = cached.get("correct", False)
                if is_correct:
                    correct += 1
                    category_correct[q.category] = category_correct.get(q.category, 0) + 1
                cached_f1 = cached.get("canonical_f1")
                if cached_f1 is not None:
                    total_f1_sum += cached_f1
                    category_f1_sum[q.category] = category_f1_sum.get(q.category, 0.0) + cached_f1
                per_question_arr[idx] = cached
                query_results_arr[idx] = QueryResult(
                    query=q.question,
                    expected=[q.answer],
                    actual=[],
                    relevant_found=0,
                    precision=cached.get("_precision", 0.0),
                    reciprocal_rank=cached.get("_reciprocal_rank", 0.0),
                    latency_ms=cached.get("_latency_ms", 0.0),
                )
                ndcg_sum += cached.get("_ndcg", 0.0)
                completed_count += 1
                qi = completed_count
                if qi == 1 or qi % 10 == 0 or qi == total_q:
                    acc_so_far = correct / qi if qi > 0 else 0.0
                    print(
                        f"  [LoCoMo] Question {qi}/{total_q} — "
                        f"accuracy: {acc_so_far:.1%} ({correct}/{qi}) [resumed]",
                        flush=True,
                    )
                return

            # Live evaluation — acquire rate-limit slot before making LLM calls.
            async with rate_limiter:
                try:
                    t0 = time.monotonic()
                    result = await self.brain.recall(q.question, bank_id=bank_id, max_results=10)
                    elapsed = (time.monotonic() - t0) * 1000
                    recall_latencies.append(elapsed)

                    # Synthesis path — always run reflect so we have the model's
                    # answer to score.
                    reflect_result = await self.brain.reflect(q.question, bank_id=bank_id)

                    canonical_f1: float | None = None
                    if llm_judge is not None:
                        llm_score = await llm_judge.score(
                            question=q.question,
                            answer=q.answer,
                            category=q.category,
                            response=reflect_result.answer,
                        )
                        is_correct = llm_score > 0.5
                    elif use_canonical_judge:
                        from astrocyte.eval.judges import locomo_score_qa

                        canonical_f1 = locomo_score_qa(
                            prediction=reflect_result.answer,
                            ground_truth=q.answer,
                            category=q.category,
                        )
                        total_f1_sum += canonical_f1
                        category_f1_sum[q.category] = (
                            category_f1_sum.get(q.category, 0.0) + canonical_f1
                        )
                        is_correct = canonical_f1 > 0.3
                    else:
                        answer_in_recall = any(
                            word_overlap_score(q.answer, h.text) > ANSWER_OVERLAP_THRESHOLD
                            for h in result.hits
                        )
                        answer_in_reflect = (
                            word_overlap_score(q.answer, reflect_result.answer)
                            > ANSWER_OVERLAP_THRESHOLD
                        )
                        is_correct = answer_in_recall or answer_in_reflect

                    if is_correct:
                        correct += 1
                        category_correct[q.category] = category_correct.get(q.category, 0) + 1

                    # Build QueryResult for standard metrics
                    relevant_ids: set[str] = set()
                    for h in result.hits:
                        if h.memory_id and word_overlap_score(q.answer, h.text) > ANSWER_OVERLAP_THRESHOLD:
                            relevant_ids.add(h.memory_id)
                    retrieved_ids = [h.memory_id for h in result.hits if h.memory_id]
                    q_precision = len(relevant_ids) / max(len(retrieved_ids), 1)
                    q_rr = next(
                        (1.0 / (i + 1) for i, rid in enumerate(retrieved_ids) if rid in relevant_ids),
                        0.0,
                    )
                    q_ndcg = ndcg_at_k(relevant_ids, retrieved_ids)
                    ndcg_sum += q_ndcg

                    q_record: dict[str, Any] = {
                        "question": q.question,
                        "expected_answer": q.answer,
                        "category": q.category,
                        "correct": is_correct,
                        "recall_hits": len(result.hits),
                        "reflect_answer_preview": reflect_result.answer[:200],
                        "canonical_f1": canonical_f1,
                        "_precision": q_precision,
                        "_reciprocal_rank": q_rr,
                        "_latency_ms": elapsed,
                        "_ndcg": q_ndcg,
                    }
                    per_question_arr[idx] = q_record
                    if checkpoint is not None:
                        checkpoint.record_question(q_key, q_record)

                    query_results_arr[idx] = QueryResult(
                        query=q.question,
                        expected=[q.answer],
                        actual=result.hits,
                        relevant_found=len(relevant_ids),
                        precision=q_precision,
                        reciprocal_rank=q_rr,
                        latency_ms=elapsed,
                    )

                except Exception as exc:
                    logging.getLogger("astrocyte.eval.locomo").warning(
                        "eval failed for q=%s: %s (counted as incorrect)",
                        q_key, exc,
                    )
                    # Leave per_question_arr[idx] as None; filter later.

            completed_count += 1
            _progress_print()

        await asyncio.gather(*[_eval_one(i, q) for i, q in enumerate(all_questions)])

        # Rebuild ordered lists from pre-allocated arrays, dropping any None
        # entries that resulted from exceptions.
        per_question: list[dict[str, Any]] = [r for r in per_question_arr if r is not None]
        query_results: list[QueryResult] = [r for r in query_results_arr if r is not None]

        # ── Phase 4: Compute results ──
        total_duration = time.monotonic() - start_time
        overall_accuracy = correct / max(len(all_questions), 1)

        category_accuracy: dict[str, float] = {}
        for cat in category_total:
            category_accuracy[cat] = category_correct.get(cat, 0) / category_total[cat]

        # Canonical F1 means — the paper's headline numbers. Only
        # populated when use_canonical_judge was True; empty dict under
        # legacy scorer so downstream serializers know this run didn't
        # produce F1 data.
        canonical_f1_overall: float | None = None
        canonical_f1_by_category: dict[str, float] = {}
        if use_canonical_judge:
            canonical_f1_overall = total_f1_sum / max(len(all_questions), 1)
            for cat in category_total:
                canonical_f1_by_category[cat] = (
                    category_f1_sum.get(cat, 0.0) / category_total[cat]
                )

        from astrocyte.eval.metrics import percentile

        metrics = EvalMetrics(
            recall_precision=sum(qr.precision for qr in query_results) / max(len(query_results), 1),
            recall_hit_rate=sum(1.0 for qr in query_results if qr.relevant_found > 0) / max(len(query_results), 1),
            recall_mrr=sum(qr.reciprocal_rank for qr in query_results) / max(len(query_results), 1),
            recall_ndcg=ndcg_sum / max(len(query_results), 1),
            retain_latency_p50_ms=percentile(retain_latencies, 50),
            retain_latency_p95_ms=percentile(retain_latencies, 95),
            recall_latency_p50_ms=percentile(recall_latencies, 50),
            recall_latency_p95_ms=percentile(recall_latencies, 95),
            total_tokens_used=getattr(getattr(self.brain, "_pipeline", None), "tokens_used", 0),
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
                logging.getLogger("astrocyte.eval").debug("Cleanup forget failed for bank %s", bank_id, exc_info=True)

        if checkpoint is not None:
            checkpoint.complete()

        return LoCoMoResult(
            overall_accuracy=overall_accuracy,
            category_accuracy=category_accuracy,
            total_questions=len(all_questions),
            correct=correct,
            per_question=per_question,
            eval_result=eval_result,
            canonical_f1_overall=canonical_f1_overall,
            canonical_f1_by_category=canonical_f1_by_category,
        )


# Paper's category numbering as verified from ``datasets/locomo/data/locomo10.json``:
#   cat 1 — multi-hop (multi-speaker synthesis, often comma-listed answers)
#   cat 2 — temporal ("when did X..." questions)
#   cat 3 — open-domain (commonsense / inference — "would X likely...")
#   cat 4 — single-hop (single-session factual)
#   cat 5 — adversarial (unanswerable; empty ground-truth answer)
#
# Previous adapter versions (v0–v5) had this mapping wrong in two places
# (the adapter labels AND the judge IDs). The per-category breakdowns
# in snapshots before this commit carry misleading names — notably the
# old "multi-hop" column actually reported temporal questions, and the
# old "single-hop" column reported multi-hop. Historical numbers are
# still real; only the labels were swapped.
_LOCOMO_CATEGORY_MAP: dict[int, str] = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}


def load_locomo_dataset(
    data_path: str | Path,
    max_conversations: int | None = None,
) -> list[LoCoMoConversation]:
    """Load LoCoMo dataset from JSON.

    Handles both:
    - Single file: locomo10.json (array of conversations)
    - Directory: looks for locomo10.json or *.json inside

    Each entry has:
    - qa: list of {question, answer, category (int 1-5), evidence}
    - conversation: {speaker_a, speaker_b, session_1, session_1_date_time, ...}
    - Session turns: {speaker, dia_id, text}

    Returns list of LoCoMoConversation objects.
    """
    data_path = Path(data_path)

    if data_path.is_dir():
        candidates = list(data_path.glob("locomo*.json")) or list(data_path.glob("*.json"))
        if not candidates:
            raise FileNotFoundError(f"No JSON files found in {data_path}")
        data_path = candidates[0]

    try:
        with open(data_path) as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Malformed JSON in LoCoMo benchmark file {data_path}: {e}") from e

    # Handle both array and single-object formats
    items = raw if isinstance(raw, list) else [raw]

    conversations: list[LoCoMoConversation] = []

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        convo_id = str(item.get("id", item.get("conversation_id", f"convo-{i}")))

        # Sessions live inside item["conversation"], not at the top level.
        convo_data = item.get("conversation", item)
        session_source = convo_data if isinstance(convo_data, dict) else item

        # Parse sessions from session_1, session_2, etc.
        sessions: list[LoCoMoSession] = []
        for key, value in session_source.items():
            if key.startswith("session_") and not key.endswith(("_observation", "_date_time", "_summary")):
                session_id = key
                date_key = f"{key}_date_time"
                date_time = session_source.get(date_key)

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

        # Parse QA — category is an integer (1-5) in the real dataset
        questions: list[LoCoMoQuestion] = []
        qa_data = item.get("qa", item.get("questions", []))
        if isinstance(qa_data, list):
            for qa in qa_data:
                if not isinstance(qa, dict) or "question" not in qa:
                    continue
                raw_category = qa.get("category", "general")
                if isinstance(raw_category, int):
                    category = _LOCOMO_CATEGORY_MAP.get(raw_category, f"category-{raw_category}")
                else:
                    category = str(raw_category)
                questions.append(
                    LoCoMoQuestion(
                        question=qa["question"],
                        answer=str(qa.get("answer", "")),
                        category=category,
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
