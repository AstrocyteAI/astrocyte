"""LongMemEval benchmark adapter.

LongMemEval (ICLR 2025) tests five long-term memory abilities:
1. Information extraction (single-session-user, single-session-assistant, single-session-preference)
2. Multi-session reasoning
3. Temporal reasoning
4. Knowledge updates
5. Abstention (question_type ending in "_abs")

Dataset: https://github.com/xiaowu0162/LongMemEval
Paper: https://arxiv.org/abs/2410.10813

Usage:
    from astrocyte.eval.benchmarks.longmemeval import LongMemEvalBenchmark

    bench = LongMemEvalBenchmark(brain)
    results = await bench.run(data_path="./LongMemEval/data", bank_id="bench-lme")
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrocyte.eval.checkpoint import BenchmarkCheckpoint
from astrocyte.eval.metrics import ndcg_at_k, text_overlap_score
from astrocyte.types import EvalMetrics, EvalResult, ForgetRequest, QueryResult

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


ANSWER_MATCH_THRESHOLD = 0.3

# Map question_type to high-level category for reporting
_CATEGORY_MAP: dict[str, str] = {
    "single-session-user": "extraction",
    "single-session-assistant": "extraction",
    "single-session-preference": "extraction",
    "multi-session": "reasoning",
    "temporal-reasoning": "temporal",
    "knowledge-update": "update",
}


def _parse_longmemeval_date(raw: str | None) -> datetime | None:
    """Parse a LongMemEval session date string to a UTC datetime.

    The dataset uses the literal form ``"2023/05/20 (Sat) 02:21"`` — a
    ``YYYY/MM/DD (DDD) HH:MM`` pattern with a parenthesised weekday in
    the middle. strptime can't handle the weekday token directly, so
    strip it and parse the rest. Returns None on any failure so the
    benchmark falls back to retain-time clock rather than crashing.
    """
    if not raw or not isinstance(raw, str):
        return None
    # Drop the parenthesised weekday: "(Sat) " → ""
    parts = raw.split()
    cleaned_parts = [p for p in parts if not (p.startswith("(") and p.endswith(")"))]
    cleaned = " ".join(cleaned_parts)
    for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _classify_question_type(question_type: str) -> str:
    """Map a LongMemEval question_type to a high-level category."""
    if question_type.endswith("_abs"):
        return "abstention"
    return _CATEGORY_MAP.get(question_type, question_type)


@dataclass
class LongMemEvalResult:
    """Results from a LongMemEval benchmark run."""

    overall_accuracy: float
    category_accuracy: dict[str, float]
    total_questions: int
    correct: int
    per_question: list[dict[str, Any]]
    eval_result: EvalResult  # Standard Astrocyte EvalResult


@dataclass
class LongMemEvalQuestion:
    """A single LongMemEval question."""

    question_id: str
    category: str  # "extraction", "reasoning", "temporal", "update", "abstention"
    question: str
    answer: str
    session_ids: list[str]
    conversation_context: list[dict[str, str]]  # Chat history
    #: Raw upstream question_type, e.g. "single-session-user",
    #: "temporal-reasoning", "knowledge-update_abs". Required by the
    #: canonical LLM-judge to pick the right prompt template;
    #: ``category`` above is the lossy Astrocyte-side summary and
    #: cannot round-trip to the template choice.
    question_type: str = ""


class LongMemEvalBenchmark:
    """Adapter for running LongMemEval against Astrocyte.

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
        max_sessions: int | None = None,
        use_canonical_judge: bool = False,
        checkpoint: BenchmarkCheckpoint | None = None,
    ) -> LongMemEvalResult:
        """Run the LongMemEval benchmark.

        Args:
            data_path: Path to LongMemEval data directory (contains .json files).
                       If None, `questions` must be provided.
            questions: Pre-loaded questions (alternative to data_path).
            bank_id: Dedicated bank for the benchmark.
            clean_after: Delete the bank after running.
            max_questions: Limit number of questions (for quick testing).
            max_sessions: Cap the retain phase at this many unique sessions.
                          Useful for cost control on large haystacks: the
                          dataset has ~1500 sessions across 500 questions,
                          so even 300-400 sessions covers the evidence for
                          most questions while halving retain cost.
                          When None (default), all sessions are retained.
            use_canonical_judge: When True, score each reflect answer
                with the canonical LongMemEval LLM-judge
                (``astrocyte.eval.judges.longmemeval_judge``) using the
                paper's task-specific prompts. Requires an LLM provider
                on ``brain._pipeline.llm_provider`` (raises if absent).
                When False (default), uses the legacy
                ``text_overlap_score`` scorer.

            checkpoint: Optional :class:`~astrocyte.eval.checkpoint.BenchmarkCheckpoint`
                from a previous interrupted run. Already-retained sessions are
                skipped and already-evaluated questions use their cached score
                without making fresh LLM calls.

        Returns:
            LongMemEvalResult with accuracy breakdown by category.
        """
        if checkpoint is not None:
            print(f"  [LongMemEval] {checkpoint.resume_summary()}")

        if questions is None and data_path is not None:
            questions = load_longmemeval_dataset(data_path, max_questions=max_questions)
        elif questions is None:
            raise ValueError("Either data_path or questions must be provided")
        elif max_questions and len(questions) > max_questions:
            # Pre-loaded questions — apply stratified sampling so the
            # caller's max_questions limit doesn't silently drop categories.
            questions = _stratified_sample(questions, max_questions)

        start_time = time.monotonic()
        retain_latencies: list[float] = []
        recall_latencies: list[float] = []

        # Reset token counter for this benchmark run
        if self.brain._pipeline:
            self.brain._pipeline.reset_token_counter()

        # ── Phase 1: Retain conversation sessions ──
        total_questions = len(questions)

        # Pre-count unique sessions so we can show progress as N/total.
        # Deduplicate by session_id (not per-question) since multiple questions
        # share the same haystack sessions.
        _all_session_keys: set[str] = set()
        for q in questions:
            for msg in q.conversation_context:
                sk = msg.get("session_id", "")
                if msg.get("content", "").strip():
                    _all_session_keys.add(sk)
        total_sessions = len(_all_session_keys)

        print(f"  [LongMemEval] Retaining {total_sessions} unique sessions from {total_questions} questions...")
        sessions_retained: set[str] = set()
        retain_count = 0
        retain_phase_start = time.monotonic()
        for q in questions:
            for msg in q.conversation_context:
                session_key = msg.get("session_id", "")
                if session_key in sessions_retained:
                    continue
                if max_sessions is not None and len(sessions_retained) >= max_sessions:
                    continue
                # Skip sessions already retained in a previous interrupted run.
                if checkpoint is not None and checkpoint.is_session_retained(session_key):
                    sessions_retained.add(session_key)
                    continue
                sessions_retained.add(session_key)

                content = msg.get("content", "")
                if not content.strip():
                    continue

                # Parse the session timestamp so the temporal retrieval
                # strategy has a real signal. Without this, every memory
                # gets the same retain-time timestamp and recency decay
                # is meaningless.
                occurred_at = _parse_longmemeval_date(msg.get("session_date"))

                t0 = time.monotonic()
                await self.brain.retain(
                    content,
                    bank_id=bank_id,
                    tags=[msg.get("role", "user"), q.category],
                    metadata={
                        "session_id": msg.get("session_id", ""),
                        "source": "longmemeval",
                        "session_date": msg.get("session_date", ""),
                    },
                    occurred_at=occurred_at,
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
                        f"  [LongMemEval] Retained {retain_count}/{total_sessions} sessions "
                        f"({elapsed_r:.0f}s elapsed, ~{remaining:.0f}s remaining)",
                        flush=True,
                    )

        print(f"  [LongMemEval] Retain complete: {retain_count} sessions stored.")

        # ── Phase 2: Evaluate questions ──
        correct = 0
        category_correct: dict[str, int] = {}
        category_total: dict[str, int] = {}
        per_question: list[dict[str, Any]] = []
        query_results: list[QueryResult] = []

        # Build the canonical LLM-judge once when requested. Reuses the
        # same LLM provider the reflect stage uses — consistent tier,
        # shared rate-limit pool, same Doppler-injected API key.
        canonical_judge = None
        if use_canonical_judge:
            from astrocyte.eval.judges import LongMemEvalJudge

            if self.brain._pipeline is None or self.brain._pipeline.llm_provider is None:
                raise RuntimeError(
                    "use_canonical_judge=True requires brain._pipeline.llm_provider "
                    "to be configured (the judge sends one LLM call per question). "
                    "Either provide an LLM or run with use_canonical_judge=False.",
                )
            canonical_judge = LongMemEvalJudge(self.brain._pipeline.llm_provider)

        ndcg_sum = 0.0
        print(f"  [LongMemEval] Evaluating {total_questions} questions...")
        eval_phase_start = time.monotonic()
        for qi, q in enumerate(questions, 1):
            category_total[q.category] = category_total.get(q.category, 0) + 1

            # Restore cached result from a previous interrupted run.
            if checkpoint is not None and checkpoint.is_question_evaluated(q.question_id):
                cached = checkpoint.get_question_result(q.question_id)
                is_correct = cached.get("correct", False)
                if is_correct:
                    correct += 1
                    category_correct[q.category] = category_correct.get(q.category, 0) + 1
                per_question.append(cached)
                # Restore placeholder QueryResult so aggregate metrics stay aligned.
                query_results.append(
                    QueryResult(
                        query=q.question,
                        expected=[q.answer],
                        actual=[],
                        relevant_found=0,
                        precision=cached.get("_precision", 0.0),
                        reciprocal_rank=cached.get("_reciprocal_rank", 0.0),
                        latency_ms=cached.get("_latency_ms", 0.0),
                    )
                )
                ndcg_sum += cached.get("_ndcg", 0.0)
                if qi == 1 or qi % 10 == 0 or qi == total_questions:
                    acc_so_far = correct / qi
                    print(
                        f"  [LongMemEval] Question {qi}/{total_questions} — "
                        f"accuracy: {acc_so_far:.1%} ({correct}/{qi}) [resumed]",
                        flush=True,
                    )
                continue

            t0 = time.monotonic()
            result = await self.brain.recall(q.question, bank_id=bank_id, max_results=10)
            elapsed = (time.monotonic() - t0) * 1000
            recall_latencies.append(elapsed)

            # Always run reflect so canonical-judge path has an answer.
            reflect_result = await self.brain.reflect(q.question, bank_id=bank_id)

            if canonical_judge is not None:
                # Canonical LLM-judge against reflect answer. Uses the
                # raw upstream question_type (not the Astrocyte-side
                # lossy category) so the prompt template matches the
                # paper's per-task rubric.
                try:
                    score = await canonical_judge.score(
                        question_type=q.question_type or q.category,
                        question=q.question,
                        answer=q.answer,
                        response=reflect_result.answer,
                    )
                    is_correct = score >= 1.0
                except Exception as exc:
                    logging.getLogger("astrocyte.eval.longmemeval").warning(
                        "canonical judge failed for q=%s: %s (counted as incorrect)",
                        q.question_id, exc,
                    )
                    is_correct = False
            else:
                # Legacy scorer — loose but historically comparable.
                answer_found = any(
                    text_overlap_score([q.answer], h.text) > ANSWER_MATCH_THRESHOLD
                    for h in result.hits
                )
                answer_in_reflect = (
                    text_overlap_score([q.answer], reflect_result.answer)
                    > ANSWER_MATCH_THRESHOLD
                )
                is_correct = answer_found or answer_in_reflect
            if is_correct:
                correct += 1
                category_correct[q.category] = category_correct.get(q.category, 0) + 1

            if qi == 1 or qi % 10 == 0 or qi == total_questions:
                acc_so_far = correct / qi
                elapsed_e = time.monotonic() - eval_phase_start
                rate_e = qi / elapsed_e if elapsed_e > 0 else 0
                remaining_e = (total_questions - qi) / rate_e if rate_e > 0 else 0
                print(
                    f"  [LongMemEval] Question {qi}/{total_questions} — "
                    f"accuracy: {acc_so_far:.1%} ({correct}/{qi}) — "
                    f"~{remaining_e:.0f}s remaining",
                    flush=True,
                )

            # Build QueryResult for standard metrics
            relevant_ids: set[str] = set()
            for h in result.hits:
                if h.memory_id and text_overlap_score([q.answer], h.text) > ANSWER_MATCH_THRESHOLD:
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
                "question_id": q.question_id,
                "category": q.category,
                "question": q.question,
                "expected_answer": q.answer,
                "correct": is_correct,
                "recall_hits": len(result.hits),
                "reflect_answer_preview": reflect_result.answer[:200],
                # Hidden fields used when restoring from checkpoint
                "_precision": q_precision,
                "_reciprocal_rank": q_rr,
                "_latency_ms": elapsed,
                "_ndcg": q_ndcg,
            }
            per_question.append(q_record)
            if checkpoint is not None:
                checkpoint.record_question(q.question_id, q_record)

            query_results.append(
                QueryResult(
                    query=q.question,
                    expected=[q.answer],
                    actual=result.hits,
                    relevant_found=len(relevant_ids),
                    precision=q_precision,
                    reciprocal_rank=q_rr,
                    latency_ms=elapsed,
                )
            )

        # ── Phase 3: Compute results ──
        total_duration = time.monotonic() - start_time
        overall_accuracy = correct / max(len(questions), 1)

        category_accuracy: dict[str, float] = {}
        for cat in category_total:
            category_accuracy[cat] = category_correct.get(cat, 0) / category_total[cat]

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
            total_tokens_used=self.brain._pipeline.tokens_used if self.brain._pipeline else 0,
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
                logging.getLogger("astrocyte.eval").debug("Cleanup forget failed for bank %s", bank_id, exc_info=True)

        if checkpoint is not None:
            checkpoint.complete()

        return LongMemEvalResult(
            overall_accuracy=overall_accuracy,
            category_accuracy=category_accuracy,
            total_questions=len(questions),
            correct=correct,
            per_question=per_question,
            eval_result=eval_result,
        )


def _stratified_sample(
    questions: list[LongMemEvalQuestion],
    max_questions: int,
) -> list[LongMemEvalQuestion]:
    """Sample up to max_questions proportionally from each category.

    Without stratification, a sorted dataset hands max_questions=200 only
    the categories that appear first, silently omitting temporal/update/
    abstention from all metrics. Stratification ensures every category
    contributes questions proportional to its share of the full dataset.
    """
    from collections import defaultdict

    by_cat: dict[str, list[LongMemEvalQuestion]] = defaultdict(list)
    for q in questions:
        by_cat[q.category].append(q)

    cats = sorted(by_cat.keys())
    n_cats = len(cats)
    if n_cats == 0:
        return []

    # Allocate quota per category proportional to its size, floor then
    # distribute remainder to the largest categories.
    total = len(questions)
    quotas: dict[str, int] = {}
    remainder = max_questions
    for cat in cats:
        share = len(by_cat[cat]) / total
        quotas[cat] = math.floor(share * max_questions)
        remainder -= quotas[cat]

    # Distribute leftover slots to categories with the most questions
    for cat in sorted(cats, key=lambda c: len(by_cat[c]), reverse=True):
        if remainder <= 0:
            break
        quotas[cat] += 1
        remainder -= 1

    result: list[LongMemEvalQuestion] = []
    for cat in cats:
        result.extend(by_cat[cat][: quotas[cat]])
    return result


def load_longmemeval_dataset(
    data_path: str | Path,
    max_questions: int | None = None,
) -> list[LongMemEvalQuestion]:
    """Load LongMemEval dataset from JSON files.

    Expects the directory structure from https://github.com/xiaowu0162/LongMemEval:
    - data_path/*.json (e.g., longmemeval_s_cleaned.json)

    Each entry has:
    - question_id, question_type, question, answer
    - haystack_sessions: list of session turn lists
    - haystack_session_ids: list of session ID strings
    - answer_session_ids: sessions containing the evidence

    Returns list of LongMemEvalQuestion objects.
    """
    data_path = Path(data_path)
    questions: list[LongMemEvalQuestion] = []

    if data_path.is_file():
        json_files = [data_path]
    elif data_path.is_dir():
        json_files = list(data_path.glob("*.json"))
        if not json_files:
            raise FileNotFoundError(f"No JSON files found in {data_path}")
    else:
        raise FileNotFoundError(f"LongMemEval path does not exist: {data_path}")

    for json_file in json_files:
        try:
            with open(json_file) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Malformed JSON in LongMemEval benchmark file {json_file}: {e}") from e

        items = data if isinstance(data, list) else [data]

        for item in items:
            if not isinstance(item, dict):
                continue

            # Extract question_type and map to category
            question_type = item.get("question_type", item.get("category", item.get("type", "general")))
            category = _classify_question_type(str(question_type))

            # Extract question and answer
            question_text = str(item.get("question", item.get("query", "")))
            answer_text = str(item.get("answer", item.get("gold_answer", "")))

            # Extract session IDs
            session_ids = item.get("haystack_session_ids", item.get("answer_session_ids", item.get("session_ids", [])))

            # Build conversation context from haystack_sessions.
            # haystack_sessions is a list of sessions, each session is a list of turns:
            #   [[ {"role": "user", "content": "..."}, {"role": "assistant", "content": "..."} ], ...]
            # We flatten into a list of turn dicts with session_id added.
            conversation_context: list[dict[str, str]] = []
            haystack_sessions = item.get("haystack_sessions", [])
            haystack_session_id_list = item.get("haystack_session_ids", [])
            # haystack_dates: one date per session in LongMemEval — e.g.
            # "2023/05/20 (Sat) 02:21". Used as ``occurred_at`` so the
            # temporal retrieval strategy can rank by real recency rather
            # than retain-time clock (which would flatten all memories
            # into the same second).
            haystack_dates_list = item.get("haystack_dates", [])

            if isinstance(haystack_sessions, list):
                for sess_idx, session_turns in enumerate(haystack_sessions):
                    sess_id = (
                        haystack_session_id_list[sess_idx]
                        if sess_idx < len(haystack_session_id_list)
                        else f"session_{sess_idx}"
                    )
                    sess_date = (
                        haystack_dates_list[sess_idx]
                        if sess_idx < len(haystack_dates_list)
                        else None
                    )
                    # Concatenate all turns of a session into a single memory
                    # entry. Previously we emitted one entry per turn and the
                    # retain loop deduped by session_id — storing the FIRST
                    # turn and discarding the rest, effectively losing 90%+
                    # of the haystack content. See
                    # docs/_design/platform-positioning.md §LongMemEval-root-causes
                    # for the full writeup.
                    if isinstance(session_turns, list):
                        turn_texts: list[str] = []
                        for turn in session_turns:
                            if isinstance(turn, dict) and turn.get("content"):
                                role = turn.get("role", "user")
                                turn_texts.append(f"{role}: {turn['content']}")
                        if turn_texts:
                            entry: dict[str, str] = {
                                "role": "session",
                                "content": "\n".join(turn_texts),
                                "session_id": str(sess_id),
                            }
                            if sess_date:
                                entry["session_date"] = str(sess_date)
                            conversation_context.append(entry)

            # Fallback: try older field names if haystack_sessions wasn't found
            if not conversation_context:
                raw_context = item.get("conversation", item.get("context", []))
                if isinstance(raw_context, list):
                    for turn in raw_context:
                        if isinstance(turn, dict) and turn.get("content"):
                            conversation_context.append(
                                {
                                    "role": turn.get("role", "user"),
                                    "content": str(turn["content"]),
                                    "session_id": turn.get("session_id", ""),
                                }
                            )

            q = LongMemEvalQuestion(
                question_id=str(item.get("question_id", item.get("id", ""))),
                category=category,
                question=question_text,
                answer=answer_text,
                session_ids=[str(s) for s in session_ids] if isinstance(session_ids, list) else [],
                conversation_context=conversation_context,
                question_type=str(question_type),
            )
            if q.question and q.answer:
                questions.append(q)

    if max_questions and len(questions) > max_questions:
        return _stratified_sample(questions, max_questions)
    return questions
