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
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrocyte.eval.checkpoint import BenchmarkCheckpoint
from astrocyte.eval.metrics import ndcg_at_k, word_overlap_score
from astrocyte.eval.metrics_collector import BenchmarkMetricsCollector
from astrocyte.eval.rate_limiter import EvalRateLimiter
from astrocyte.pipeline.tasks import COMPILE_PERSONA_PAGE, MemoryTask
from astrocyte.pipeline.temporal import temporal_metadata
from astrocyte.types import EvalMetrics, EvalResult, ForgetRequest, QueryResult, RetainRequest

# Minimum text overlap score to consider an answer correct.
# Tuned empirically: 0.3 balances recall (catching paraphrases) against
# precision (rejecting unrelated text). See eval/metrics.py for scoring details.
ANSWER_OVERLAP_THRESHOLD = 0.3


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Keep benchmark hit diagnostics compact and JSON-serializable."""

    if not metadata:
        return {}
    keys = (
        "source",
        "conversation_id",
        "session_id",
        "date_time",
        "session_date",
        "_created_at",
        "_obs_source_ids",
        "_wiki_source_ids",
    )
    return {key: _json_safe(metadata[key]) for key in keys if key in metadata}


def _hit_debug(hit: Any) -> dict[str, Any]:
    return {
        "memory_id": getattr(hit, "memory_id", None),
        "score": getattr(hit, "score", None),
        "fact_type": getattr(hit, "fact_type", None),
        "memory_layer": getattr(hit, "memory_layer", None),
        "tags": list(getattr(hit, "tags", None) or []),
        "metadata": _safe_metadata(getattr(hit, "metadata", None)),
        "occurred_at": _iso_or_none(getattr(hit, "occurred_at", None)),
        "retained_at": _iso_or_none(getattr(hit, "retained_at", None)),
        "text_preview": str(getattr(hit, "text", ""))[:240],
    }


def _evidence_id_hit(evidence_ids: list[str], hits: list[Any]) -> bool:
    """Best-effort match against LoCoMo evidence ids and retained metadata."""

    expected = {str(eid).lower() for eid in evidence_ids if eid}
    if not expected:
        return False
    for hit in hits:
        metadata = getattr(hit, "metadata", None) or {}
        tags = {str(tag).lower() for tag in (getattr(hit, "tags", None) or [])}
        candidates = set(tags)
        for key in ("session_id", "conversation_id", "locomo_turn_ids"):
            if metadata.get(key):
                value = str(metadata[key]).lower()
                candidates.add(value)
                candidates.add(f"{key.split('_')[0]}:{metadata[key]}".lower())
                candidates.update(part.strip() for part in value.replace("|", ",").split(",") if part.strip())
        if expected & candidates:
            return True
    return False


def _session_person_names(turns: list[dict[str, str]]) -> list[str]:
    """Extract deterministic person/name metadata for LoCoMo rerank support."""

    names: set[str] = set()
    for turn in turns:
        speaker = turn.get("speaker")
        if speaker and speaker != "unknown":
            names.add(speaker)
        text = turn.get("text", "")
        for match in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b", text):
            name = match.group(0)
            if name.lower() not in {"i", "the", "a"}:
                names.add(name)
    return sorted(names)


def _persona_pages_for_session(
    conversation_id: str,
    session_id: str,
    turns: list[dict[str, str]],
) -> dict[str, str]:
    """Compile per-person LoCoMo page snippets with source provenance."""

    by_speaker: dict[str, list[str]] = {}
    for turn in turns:
        speaker = turn.get("speaker", "unknown")
        text = turn.get("text", "").strip()
        if not text or speaker == "unknown":
            continue
        turn_id = turn.get("dia_id") or turn.get("turn_id") or session_id
        by_speaker.setdefault(speaker, []).append(f"- [{turn_id}] {text}")

    pages: dict[str, str] = {}
    for speaker, statements in by_speaker.items():
        pages[speaker] = "\n".join(
            [
                f"# Person page: {speaker}",
                f"Conversation: {conversation_id}",
                f"Session: {session_id}",
                "",
                "Stable persona/preference evidence:",
                *statements,
            ]
        )
    return pages


def _persona_batch_for_session(
    conversation_id: str,
    session_id: str,
    turns: list[dict[str, str]],
) -> tuple[list[str], str] | None:
    """Return one batched persona evidence document for a session."""

    pages = _persona_pages_for_session(conversation_id, session_id, turns)
    if not pages:
        return None
    speakers = sorted(pages)
    text = "\n\n".join(pages[speaker] for speaker in speakers)
    return speakers, text


def _persona_retain_request(
    *,
    bank_id: str,
    conversation_id: str,
    session_id: str,
    turns: list[dict[str, str]],
    source_ids: list[str],
    occurred_at: datetime | None,
) -> RetainRequest | None:
    persona_batch = _persona_batch_for_session(conversation_id, session_id, turns)
    if persona_batch is None:
        return None
    speakers, page_text = persona_batch
    return RetainRequest(
        content=page_text,
        bank_id=bank_id,
        tags=[
            "locomo",
            "persona",
            *(f"person:{speaker.lower()}" for speaker in speakers),
            f"convo:{conversation_id}",
        ],
        metadata={
            "source": "locomo_persona_compile",
            "person": ",".join(speakers),
            "locomo_persons": ",".join(speakers),
            "conversation_id": conversation_id,
            "session_id": session_id,
            "_wiki_source_ids": json.dumps(source_ids),
            "locomo_persona_page": ",".join(f"person:{speaker}" for speaker in speakers),
        },
        occurred_at=occurred_at,
        content_type="document",
        extraction_profile="locomo_persona",
    )


def _persona_compile_tasks_for_session(
    *,
    bank_id: str,
    conversation_id: str,
    session_id: str,
    turns: list[dict[str, str]],
    source_ids: list[str],
) -> list[MemoryTask]:
    """Generate one ``compile_persona_page`` task per (conversation, speaker).

    Each LoCoMo conversation is an independent storyline — "Caroline" in
    conversation_0 is a different person from "Caroline" in conversation_3.
    Scoping persona pages by ``conversation_id`` keeps their personas
    distinct (otherwise the upsert-by-name collapses all 10 conversations'
    Carolines into one diluted page, which observably regresses multi-hop
    and adversarial scores on LoCoMo).
    """
    pages = _persona_pages_for_session(conversation_id, session_id, turns)
    tasks: list[MemoryTask] = []
    for speaker in sorted(pages):
        key_speaker = re.sub(r"[^a-z0-9]+", "-", speaker.lower()).strip("-") or "unknown"
        tasks.append(
            MemoryTask(
                task_type=COMPILE_PERSONA_PAGE,
                bank_id=bank_id,
                payload={
                    "person": speaker,
                    "scope": f"convo:{conversation_id}",
                    "source_ids": source_ids,
                    "index_vector": True,
                },
                idempotency_key=(
                    f"locomo:persona:{bank_id}:{conversation_id}:{session_id}:{key_speaker}"
                ),
            )
        )
    return tasks

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
        max_questions_per_conversation: int | None = None,
        use_canonical_judge: bool = False,
        llm_judge: object | None = None,
        checkpoint: BenchmarkCheckpoint | None = None,
        retain_concurrency: int = 10,
        retain_rpm: int = 500,
        retain_tpm: int = 200_000,
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
            max_questions: Hard cap on total questions evaluated. Applied
                AFTER ``max_questions_per_conversation``. Uses a
                deterministic head-slice — questions retain their original
                order, so per-category counts can be skewed when the cap
                is small (early conversations dominate). Use
                ``max_questions_per_conversation`` for fair coverage.
            max_questions_per_conversation: Per-conversation cap. Takes
                the first N questions from EACH conversation, preserving
                category balance and giving every conversation equal
                weight in the resulting sample. Recommended for fast
                iteration benches — at 20 per conversation × 10
                conversations = 200 questions with uniform coverage.
                Combine with ``max_questions`` to also cap total size
                if needed.
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
            retain_concurrency: Maximum number of retain calls in flight.
            retain_rpm: Requests-per-minute budget for the retain rate limiter.
            retain_tpm: Tokens-per-minute budget for the retain rate limiter.
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
        reflect_latencies: list[float] = []

        # Reset token counter for this benchmark run
        if self.brain._pipeline:
            self.brain._pipeline.reset_token_counter()

        # ── Tier-3 metrics collector ──
        # Captures cost, robustness, cascade decisions, and per-question
        # signal across the bench. Installed on the brain + pipeline +
        # entity_resolver so all hot paths can record. Pipelines that don't
        # see the attribute simply no-op — production code stays untouched.
        metrics_collector = BenchmarkMetricsCollector()
        if self.brain._pipeline is not None:
            setattr(self.brain._pipeline, "_metrics_collector", metrics_collector)
            tracker = getattr(self.brain._pipeline, "_tracker", None)
            if tracker is not None:
                setattr(tracker, "_metrics_collector", metrics_collector)
            resolver = getattr(self.brain._pipeline, "entity_resolver", None)
            if resolver is not None:
                resolver.metrics_collector = metrics_collector
        metrics_collector.set_phase("retain")

        # ── Phase 1: Retain all conversation sessions ──
        total_sessions = sum(len(c.sessions) for c in conversations)
        session_work: list[tuple[LoCoMoConversation, LoCoMoSession, str]] = []
        for convo in conversations:
            for session in convo.sessions:
                session_key = f"{convo.conversation_id}:{session.session_id}"
                if checkpoint is not None and checkpoint.is_session_retained(session_key):
                    continue
                session_work.append((convo, session, session_key))

        skipped_sessions = total_sessions - len(session_work)
        skip_suffix = f" ({skipped_sessions} skipped by checkpoint)" if skipped_sessions else ""
        print(
            f"  [LoCoMo] Retaining {len(session_work)}/{total_sessions} sessions "
            f"from {len(conversations)} conversations with concurrency={retain_concurrency}{skip_suffix}...",
            flush=True,
        )
        retain_count = 0
        date_parse_attempts = 0
        unparseable_date_count = 0
        retain_phase_start = time.monotonic()
        retain_rate_limiter = EvalRateLimiter(
            max_concurrency=max(1, retain_concurrency),
            rpm=retain_rpm,
            tpm=retain_tpm,
        )
        persona_task_queue = getattr(self.brain, "_benchmark_task_queue", None)
        deferred_persona_tasks: list[MemoryTask] = []
        deferred_persona_requests: list[RetainRequest] = []

        def _session_text_and_metadata(
            convo: LoCoMoConversation,
            session: LoCoMoSession,
        ) -> tuple[str | None, dict[str, Any], datetime | None, int, int]:
            session_text_parts = []
            speaker_counts: Counter[str] = Counter()
            turn_ids: list[str] = []
            turn_speakers: list[str] = []
            for turn in session.turns:
                speaker = turn.get("speaker", "unknown")
                text = turn.get("text", "")
                if text.strip():
                    turn_id = turn.get("dia_id") or turn.get("turn_id") or ""
                    speaker_counts[speaker] += 1
                    if turn_id:
                        turn_ids.append(str(turn_id))
                        turn_speakers.append(f"{turn_id}:{speaker}")
                        session_text_parts.append(f"[{turn_id}] {speaker}: {text}")
                    else:
                        session_text_parts.append(f"{speaker}: {text}")

            if not session_text_parts:
                return None, {}, None, 0, 0

            session_text = "\n".join(session_text_parts)
            occurred_at = None
            parse_attempts = 0
            unparseable_count = 0
            if session.date_time:
                parse_attempts = 1
                date_formats = (
                    "%I:%M %p on %d %B, %Y",  # "10:04 am on 19 December, 2023" (LoCoMo native)
                    "%B %d, %Y",               # "January 15, 2026"
                    "%Y-%m-%d",                # "2026-01-15"
                    "%m/%d/%Y",                # "01/15/2026"
                    "%d %B %Y",                # "15 January 2026"
                )
                for fmt in date_formats:
                    try:
                        occurred_at = datetime.strptime(session.date_time, fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                if occurred_at is None:
                    unparseable_count = 1
                    logging.getLogger("astrocyte.eval").debug(
                        "LoCoMo: failed to parse session date_time '%s' for "
                        "conversation_id=%s session_id=%s; supported formats=%s",
                        session.date_time,
                        convo.conversation_id,
                        session.session_id,
                        date_formats,
                    )

            metadata = {
                "source": "locomo",
                "extraction_profile": "locomo_conversation",
                "conversation_id": convo.conversation_id,
                "session_id": session.session_id,
                "date_time": session.date_time or "",
                "locomo_speakers": ",".join(sorted(speaker_counts)),
                "locomo_persons": ",".join(_session_person_names(session.turns)),
                "locomo_turn_ids": ",".join(turn_ids),
                "locomo_turn_speakers": "|".join(turn_speakers),
                "locomo_turn_count": len(session.turns),
                "locomo_speaker_counts": ",".join(
                    f"{speaker}:{count}" for speaker, count in sorted(speaker_counts.items())
                ),
            }
            metadata.update(temporal_metadata(session_text, occurred_at))
            return session_text, metadata, occurred_at, parse_attempts, unparseable_count

        async def _retain_session(
            convo: LoCoMoConversation,
            session: LoCoMoSession,
            session_key: str,
        ) -> tuple[str, float, int, int] | None:
            session_text, metadata, occurred_at, parse_attempts, unparseable_count = _session_text_and_metadata(
                convo,
                session,
            )
            if session_text is None:
                return None

            t0 = time.monotonic()
            async with retain_rate_limiter:
                raw_result = await self.brain.retain(
                    session_text,
                    bank_id=bank_id,
                    tags=["locomo", f"convo:{convo.conversation_id}", f"session:{session.session_id}"],
                    metadata=metadata,
                    occurred_at=occurred_at,
                    content_type="conversation",
                    extraction_profile="locomo_conversation",
                )
            source_ids = [raw_result.memory_id] if raw_result.memory_id else []
            if persona_task_queue is not None:
                deferred_persona_tasks.extend(_persona_compile_tasks_for_session(
                    bank_id=bank_id,
                    conversation_id=convo.conversation_id,
                    session_id=session.session_id,
                    turns=session.turns,
                    source_ids=source_ids,
                ))
            else:
                persona_request = _persona_retain_request(
                    bank_id=bank_id,
                    conversation_id=convo.conversation_id,
                    session_id=session.session_id,
                    turns=session.turns,
                    source_ids=source_ids,
                    occurred_at=occurred_at,
                )
                if persona_request is not None:
                    deferred_persona_requests.append(persona_request)
            return session_key, (time.monotonic() - t0) * 1000, parse_attempts, unparseable_count

        async def _retain_pipeline_batch(
            batch: list[tuple[LoCoMoConversation, LoCoMoSession, str]],
        ) -> list[tuple[str, float, int, int] | None]:
            pipeline = getattr(self.brain, "_pipeline", None)
            if pipeline is None or not hasattr(pipeline, "retain_many"):
                return [await _retain_session(convo, session, session_key) for convo, session, session_key in batch]

            prepared_sessions: list[dict[str, Any]] = []
            raw_requests: list[RetainRequest] = []
            t0 = time.monotonic()
            for convo, session, session_key in batch:
                session_text, metadata, occurred_at, parse_attempts, unparseable_count = _session_text_and_metadata(
                    convo,
                    session,
                )
                if session_text is None:
                    prepared_sessions.append({"session_key": session_key, "skip": True})
                    continue
                raw_requests.append(
                    RetainRequest(
                        content=session_text,
                        bank_id=bank_id,
                        tags=["locomo", f"convo:{convo.conversation_id}", f"session:{session.session_id}"],
                        metadata=metadata,
                        occurred_at=occurred_at,
                        content_type="conversation",
                        extraction_profile="locomo_conversation",
                    )
                )
                prepared_sessions.append({
                    "convo": convo,
                    "session": session,
                    "session_key": session_key,
                    "occurred_at": occurred_at,
                    "parse_attempts": parse_attempts,
                    "unparseable_count": unparseable_count,
                })

            if raw_requests:
                async with retain_rate_limiter:
                    raw_results = await pipeline.retain_many(raw_requests)
            else:
                raw_results = []
            raw_results_iter = iter(raw_results)
            for index, prepared_session in enumerate(prepared_sessions):
                if prepared_session.get("skip"):
                    continue
                raw_result = next(raw_results_iter)
                convo = prepared_session["convo"]
                session = prepared_session["session"]
                source_ids = [raw_result.memory_id] if raw_result.memory_id else []
                if persona_task_queue is not None:
                    deferred_persona_tasks.extend(_persona_compile_tasks_for_session(
                        bank_id=bank_id,
                        conversation_id=convo.conversation_id,
                        session_id=session.session_id,
                        turns=session.turns,
                        source_ids=source_ids,
                    ))
                else:
                    persona_request = _persona_retain_request(
                        bank_id=bank_id,
                        conversation_id=convo.conversation_id,
                        session_id=session.session_id,
                        turns=session.turns,
                        source_ids=source_ids,
                        occurred_at=prepared_session["occurred_at"],
                    )
                    if persona_request is not None:
                        deferred_persona_requests.append(persona_request)

            elapsed_ms = (time.monotonic() - t0) * 1000
            results_for_batch: list[tuple[str, float, int, int] | None] = []
            for prepared_session in prepared_sessions:
                if prepared_session.get("skip"):
                    results_for_batch.append(None)
                else:
                    results_for_batch.append((
                        prepared_session["session_key"],
                        elapsed_ms,
                        int(prepared_session["parse_attempts"]),
                        int(prepared_session["unparseable_count"]),
                    ))
            return results_for_batch

        work_queue: asyncio.Queue[tuple[LoCoMoConversation, LoCoMoSession, str]] = asyncio.Queue()
        for item in session_work:
            work_queue.put_nowait(item)

        progress_lock = asyncio.Lock()

        async def _record_retained(
            retained: tuple[str, float, int, int] | None,
        ) -> None:
            nonlocal retain_count, date_parse_attempts, unparseable_date_count
            if retained is None:
                return
            session_key, latency_ms, parse_attempts, unparseable_count = retained
            async with progress_lock:
                retain_latencies.append(latency_ms)
                metrics_collector.record_retain_latency(latency_ms)
                date_parse_attempts += parse_attempts
                unparseable_date_count += unparseable_count
                retain_count += 1
                if checkpoint is not None:
                    checkpoint.record_session(session_key)
                if retain_count % 20 == 0 or retain_count == len(session_work):
                    elapsed_r = time.monotonic() - retain_phase_start
                    rate = retain_count / elapsed_r
                    remaining = (len(session_work) - retain_count) / rate if rate > 0 else 0
                    print(
                        f"  [LoCoMo] Retained {retain_count}/{len(session_work)} sessions "
                        f"({elapsed_r:.0f}s elapsed, ~{remaining:.0f}s remaining)",
                        flush=True,
                    )

        async def _retain_worker() -> None:
            while True:
                try:
                    convo, session, session_key = work_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await _record_retained(await _retain_session(convo, session, session_key))
                finally:
                    work_queue.task_done()

        pipeline = getattr(self.brain, "_pipeline", None)
        batch_size = max(1, retain_concurrency)
        if pipeline is not None and hasattr(pipeline, "retain_many"):
            for start in range(0, len(session_work), batch_size):
                retained_batch = await _retain_pipeline_batch(session_work[start:start + batch_size])
                for retained in retained_batch:
                    await _record_retained(retained)
        else:
            worker_count = min(batch_size, len(session_work))
            workers = [asyncio.create_task(_retain_worker()) for _ in range(worker_count)]
            if workers:
                await asyncio.gather(*workers)

        print(f"  [LoCoMo] Retain complete: {retain_count} sessions stored.")
        if unparseable_date_count > 0:
            logging.getLogger("astrocyte.eval").warning(
                "LoCoMo: %d/%d session date_time values were unparseable during retain phase. "
                "Consider extending supported date formats.",
                unparseable_date_count,
                date_parse_attempts,
            )

        # Phase boundary: retain finished, persona-compile begins.
        metrics_collector.set_phase("persona_compile")

        if persona_task_queue is not None and deferred_persona_tasks:
            print(
                f"  [LoCoMo] Enqueuing {len(deferred_persona_tasks)} persona compile tasks via PgQueuer...",
                flush=True,
            )
            for task in deferred_persona_tasks:
                await persona_task_queue.enqueue(task)
        elif deferred_persona_requests:
            print(
                f"  [LoCoMo] Running deferred persona retain phase for "
                f"{len(deferred_persona_requests)} documents...",
                flush=True,
            )
            pipeline = getattr(self.brain, "_pipeline", None)
            if pipeline is not None and hasattr(pipeline, "retain_many"):
                for start in range(0, len(deferred_persona_requests), batch_size):
                    async with retain_rate_limiter:
                        await pipeline.retain_many(deferred_persona_requests[start:start + batch_size])
            else:
                for request in deferred_persona_requests:
                    async with retain_rate_limiter:
                        await self.brain.retain(
                            request.content,
                            bank_id=request.bank_id,
                            tags=request.tags,
                            metadata=request.metadata,
                            occurred_at=request.occurred_at,
                            content_type=request.content_type,
                            extraction_profile=request.extraction_profile,
                        )

        # ── Phase 2: Collect all questions ──
        # Per-conversation sampling FIRST (preserves category/conversation
        # balance), then the optional total cap. With both unset, all
        # questions are evaluated.
        all_questions: list[LoCoMoQuestion] = []
        if max_questions_per_conversation is not None:
            for convo in conversations:
                all_questions.extend(convo.questions[:max_questions_per_conversation])
        else:
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

        # Phase boundary: persona-compile finished, eval begins.
        metrics_collector.set_phase("eval")

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
                    relevant_found=cached.get("_relevant_found", 0),
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
                    # Scope retrieval to the question's conversation. All 10
                    # LoCoMo conversations share a single bank_id, so without
                    # this tag filter recall pulls cross-talk from unrelated
                    # storylines (e.g. Caroline-in-conv-0 returning hits from
                    # Caroline-in-conv-7). Persona pages carry the same
                    # ``convo:<id>`` tag so they remain visible to scoped
                    # recall.
                    convo_tags = [f"convo:{q.conversation_id}"]
                    t0 = time.monotonic()
                    result = await self.brain.recall(
                        q.question,
                        bank_id=bank_id,
                        max_results=10,
                        tags=convo_tags,
                    )
                    elapsed = (time.monotonic() - t0) * 1000
                    recall_latencies.append(elapsed)
                    metrics_collector.record_recall_latency(elapsed)

                    # Synthesis path — always run reflect so we have the model's
                    # answer to score. Same tag scope as recall above.
                    reflect_t0 = time.monotonic()
                    reflect_result = await self.brain.reflect(
                        q.question, bank_id=bank_id, tags=convo_tags
                    )
                    reflect_elapsed = (time.monotonic() - reflect_t0) * 1000
                    reflect_latencies.append(reflect_elapsed)
                    metrics_collector.record_reflect_latency(reflect_elapsed)

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

                    # Tier-3: per-question record for recall-vs-reflect gap
                    # analysis + abstention detection.
                    answer_in_recall_for_metrics = any(
                        word_overlap_score(q.answer, h.text) > ANSWER_OVERLAP_THRESHOLD
                        for h in result.hits[:10]
                    )
                    answer_in_reflect_for_metrics = (
                        word_overlap_score(q.answer, reflect_result.answer)
                        > ANSWER_OVERLAP_THRESHOLD
                    )
                    abstain_markers = (
                        "insufficient evidence",
                        "i don't have",
                        "i do not have",
                        "not available in my memories",
                    )
                    abstained = any(
                        marker in (reflect_result.answer or "").lower()
                        for marker in abstain_markers
                    )
                    metrics_collector.record_question(
                        idx=idx,
                        category=q.category,
                        correct=is_correct,
                        recall_hit=answer_in_recall_for_metrics,
                        reflect_hit=answer_in_reflect_for_metrics,
                        abstained=abstained,
                        recall_latency_ms=elapsed,
                        reflect_latency_ms=reflect_elapsed,
                    )

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
                    recall_debug = [_hit_debug(h) for h in result.hits[:10]]
                    reflect_sources = [_hit_debug(h) for h in (reflect_result.sources or [])[:18]]
                    evidence_hit = _evidence_id_hit(
                        q.evidence_ids,
                        list(result.hits) + list(reflect_result.sources or []),
                    )

                    q_record: dict[str, Any] = {
                        "question": q.question,
                        "expected_answer": q.answer,
                        "category": q.category,
                        "evidence_ids": list(q.evidence_ids),
                        "correct": is_correct,
                        "recall_hits": len(result.hits),
                        "recall_top_hits": recall_debug,
                        "reflect_sources": reflect_sources,
                        "reflect_answer_preview": reflect_result.answer[:200],
                        "canonical_f1": canonical_f1,
                        "_precision": q_precision,
                        "_reciprocal_rank": q_rr,
                        "_latency_ms": elapsed,
                        "_ndcg": q_ndcg,
                        "_relevant_found": len(relevant_ids),
                        "_evidence_id_hit": evidence_hit,
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
                    metrics_collector.record_error(_classify_error(exc))
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

        base_metrics = EvalMetrics(
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
        # Tier-3 finalize: snapshot wiki-page state then decorate metrics.
        try:
            wiki_pages_count, unique_personas = await _wiki_page_stats(
                self.brain, bank_id,
            )
            metrics_collector.set_wiki_page_stats(
                total=wiki_pages_count,
                unique_personas=unique_personas,
            )
        except Exception as exc:
            logging.getLogger("astrocyte.eval.locomo").debug(
                "wiki page stats unavailable: %s", exc,
            )
        metrics = metrics_collector.finalize(base_metrics)
        # Detach the collector from the pipeline so subsequent runs / live
        # traffic don't accumulate into this run's bucket.
        if self.brain._pipeline is not None:
            for attr in ("_metrics_collector",):
                if hasattr(self.brain._pipeline, attr):
                    delattr(self.brain._pipeline, attr)
            tracker = getattr(self.brain._pipeline, "_tracker", None)
            if tracker is not None and hasattr(tracker, "_metrics_collector"):
                delattr(tracker, "_metrics_collector")
            resolver = getattr(self.brain._pipeline, "entity_resolver", None)
            if resolver is not None:
                resolver.metrics_collector = None

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


def _classify_error(exc: BaseException) -> str:
    """Bucket an exception into a metrics category.

    Common buckets: ``"pool_timeout"``, ``"deadlock"``, ``"openai_429"``,
    ``"openai_5xx"``, ``"timeout"``, ``"other"``. Looks at exception type
    name and message — defensive on import (no hard dependency on psycopg
    / openai exception classes).
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "pooltimeout" in name or "couldn't get a connection" in msg:
        return "pool_timeout"
    if "deadlock" in name or "deadlock detected" in msg:
        return "deadlock"
    if "ratelimit" in name or "429" in msg or "rate limit" in msg:
        return "openai_429"
    if "internal server error" in msg or "service unavailable" in msg or "502" in msg or "503" in msg or "504" in msg:
        return "openai_5xx"
    if "timeout" in name or "timeout" in msg:
        return "timeout"
    return "other"


async def _wiki_page_stats(brain: Astrocyte, bank_id: str) -> tuple[int, int]:
    """Return (total wiki pages, unique persona names) for the bank.

    Used by the metrics collector to compute pages-per-persona density —
    a higher ratio indicates per-conversation scoping is active. Falls
    back to (0, 0) when no wiki store is configured. Probes via
    ``list_pages`` rather than direct SQL so the helper works against any
    wiki-store SPI implementation.
    """
    pipeline = getattr(brain, "_pipeline", None)
    wiki_store = getattr(pipeline, "wiki_store", None) if pipeline else None
    if wiki_store is None or not hasattr(wiki_store, "list_pages"):
        return 0, 0
    try:
        pages = await wiki_store.list_pages(bank_id, kind="entity")
    except Exception:
        return 0, 0
    total = len(pages or [])
    if total == 0:
        return 0, 0
    unique_personas: set[str] = set()
    for page in pages:
        # Page IDs look like "person:{slug}" or "person:{scope-slug}:{slug}".
        # Strip the trailing slug after the last ":" — that's the persona.
        page_id = getattr(page, "page_id", "") or ""
        if ":" in page_id:
            persona = page_id.rsplit(":", 1)[1]
            unique_personas.add(persona)
    return total, len(unique_personas)


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
                                    "dia_id": str(turn.get("dia_id", turn.get("id", ""))),
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
