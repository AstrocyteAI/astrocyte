#!/usr/bin/env python3
"""Run LongMemEval and LoCoMo benchmarks against Astrocyte.

Usage:
    # With real providers (requires OPENAI_API_KEY or similar):
    python scripts/run_benchmarks.py --config benchmarks/config.yaml

    # Quick smoke test with in-memory providers:
    python scripts/run_benchmarks.py --provider test --max-questions 10

    # Full LongMemEval only:
    python scripts/run_benchmarks.py --config benchmarks/config.yaml \
        --benchmarks longmemeval --longmemeval-path ./LongMemEval/data

    # Full LoCoMo only:
    python scripts/run_benchmarks.py --config benchmarks/config.yaml \
        --benchmarks locomo --locomo-path ./locomo/data

Environment variables:
    OPENAI_API_KEY          Required for real LLM/embedding providers.
    BENCHMARK_RESULTS_DIR   Output directory (default: benchmark-results).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# psycopg pool emits WARNING-level "rolling back returned connection" messages
# when the pgvector adapter returns connections without explicitly ending their
# read transactions. The pool cleans up correctly; these messages are noise.
logging.getLogger("psycopg.pool").setLevel(logging.ERROR)


@dataclass
class BenchmarkRunOutcome:
    """Result of running a benchmark: serialized payload plus dataset provenance."""

    result: dict | None
    used_real_data: bool


def _build_test_brain(*, enable_multi_query_expansion: bool = False):
    """Create an Astrocyte instance with in-memory pipeline (no API keys needed).

    Uses InMemoryVectorStore + InMemoryDocumentStore + MockLLMProvider with
    bag-of-words embeddings so the full pipeline path (chunk → embed →
    store → 4-way retrieve → fuse → rerank) is exercised. The document
    store is included so RRF has keyword/BM25 as a fusion input alongside
    semantic + temporal — see Session 1 Item 2 of the platform-positioning
    LongMemEval root-causes writeup.
    """
    from astrocyte._astrocyte import Astrocyte
    from astrocyte.config import AstrocyteConfig
    from astrocyte.pipeline.orchestrator import PipelineOrchestrator
    from astrocyte.testing.in_memory import (
        InMemoryDocumentStore,
        InMemoryVectorStore,
        MockLLMProvider,
    )

    config = AstrocyteConfig()
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    pipeline = PipelineOrchestrator(
        vector_store=InMemoryVectorStore(),
        document_store=InMemoryDocumentStore(),
        llm_provider=MockLLMProvider(),
        enable_multi_query_expansion=enable_multi_query_expansion,
    )
    brain.set_pipeline(pipeline)
    return brain


def _build_pipeline_brain(config_path: str, *, enable_multi_query_expansion: bool = False):
    """Create an Astrocyte instance with Tier 1 pipeline from YAML config.

    The config should specify vector_store, llm_provider, etc.
    Provider resolution uses entry points or direct import paths.
    """
    from astrocyte._astrocyte import Astrocyte
    from astrocyte._discovery import resolve_provider
    from astrocyte.config import load_config
    from astrocyte.pipeline.orchestrator import PipelineOrchestrator

    config = load_config(config_path)
    brain = Astrocyte(config)

    # Resolve and instantiate providers
    if config.vector_store:
        vs_cls = resolve_provider(config.vector_store, "vector_stores")
        vector_store = vs_cls(**(config.vector_store_config or {}))
    else:
        from astrocyte.testing.in_memory import InMemoryVectorStore

        vector_store = InMemoryVectorStore()

    graph_store = None
    if config.graph_store:
        gs_cls = resolve_provider(config.graph_store, "graph_stores")
        graph_store = gs_cls(**(config.graph_store_config or {}))

    document_store = None
    if config.document_store:
        ds_cls = resolve_provider(config.document_store, "document_stores")
        document_store = ds_cls(**(config.document_store_config or {}))
    else:
        # Default to in-memory document store so keyword/BM25 retrieval
        # fires for benchmark runs even when the user's config doesn't
        # explicitly name a document_store provider. Production
        # deployments should configure a real one (Elasticsearch, etc.)
        # via config.document_store.
        from astrocyte.testing.in_memory import InMemoryDocumentStore

        document_store = InMemoryDocumentStore()

    if config.llm_provider:
        llm_cls = resolve_provider(config.llm_provider, "llm_providers")
        llm_provider = llm_cls(**(config.llm_provider_config or {}))
    else:
        from astrocyte.testing.in_memory import MockLLMProvider

        llm_provider = MockLLMProvider()

    pipeline = PipelineOrchestrator(
        vector_store=vector_store,
        llm_provider=llm_provider,
        graph_store=graph_store,
        document_store=document_store,
        enable_multi_query_expansion=enable_multi_query_expansion,
    )
    brain.set_pipeline(pipeline)
    return brain


def _serialize_result(
    result,
    benchmark_name: str,
    *,
    judge: str = "legacy",
    system: str = "astrocyte",
) -> dict:
    """Convert benchmark result to a JSON-serializable dict.

    ``judge`` records which scoring method produced the numbers so
    downstream consumers (positioning doc, CI regression gate,
    competitor matrix) can tell apples-to-apples from apples-to-oranges:

    - ``"legacy"`` — Astrocyte's pre-canonical scorer
      (``word_overlap_score > 0.3`` for LoCoMo, ``text_overlap_score``
      for LongMemEval). Useful for internal v-to-v delta tracking;
      NOT comparable with published competitor numbers.
    - ``"canonical"`` — the paper's reference judge. LoCoMo: stemmed
      token-F1 via ``astrocyte.eval.judges.locomo_judge``. LongMemEval:
      LLM-judge via ``astrocyte.eval.judges.longmemeval_judge``.
      REQUIRED for cross-system comparisons.

    ``system`` identifies what produced the predictions. Defaults to
    ``"astrocyte"``; competitor adapter runs set it to
    ``"mem0"`` / ``"zep"`` / etc. so a head-to-head matrix can filter
    and group cleanly without re-parsing filenames.
    """
    data = {
        "benchmark": benchmark_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "system": system,
        "judge": judge,
        "overall_accuracy": result.overall_accuracy,
        "category_accuracy": result.category_accuracy,
        "total_questions": result.total_questions,
        "correct": result.correct,
        "metrics": {
            "recall_precision": result.eval_result.metrics.recall_precision,
            "recall_hit_rate": result.eval_result.metrics.recall_hit_rate,
            "recall_mrr": result.eval_result.metrics.recall_mrr,
            "recall_ndcg": result.eval_result.metrics.recall_ndcg,
            "retain_latency_p50_ms": result.eval_result.metrics.retain_latency_p50_ms,
            "retain_latency_p95_ms": result.eval_result.metrics.retain_latency_p95_ms,
            "recall_latency_p50_ms": result.eval_result.metrics.recall_latency_p50_ms,
            "recall_latency_p95_ms": result.eval_result.metrics.recall_latency_p95_ms,
            "reflect_accuracy": result.eval_result.metrics.reflect_accuracy,
            "total_tokens_used": result.eval_result.metrics.total_tokens_used,
            "total_duration_seconds": result.eval_result.metrics.total_duration_seconds,
        },
        "provider": result.eval_result.provider,
        "provider_tier": result.eval_result.provider_tier,
    }
    # Canonical F1 means — only populated on LoCoMo canonical-judge runs
    # (attribute exists on LoCoMoResult; None under legacy scorer).
    # Exposed as the primary cross-competitor metric, since the paper
    # reports F1 MEANS (not pass/fail counts) as headline numbers.
    f1_overall = getattr(result, "canonical_f1_overall", None)
    if f1_overall is not None:
        data["canonical_f1_overall"] = f1_overall
        data["canonical_f1_by_category"] = getattr(
            result, "canonical_f1_by_category", {},
        )
    return data


def _print_result(result, benchmark_name: str) -> None:
    """Print benchmark results to stdout."""
    print(f"\n{'=' * 60}")
    print(f"  {benchmark_name}")
    print(f"{'=' * 60}")
    print(f"  Overall accuracy:  {result.overall_accuracy:.1%}")
    print(f"  Questions:         {result.correct}/{result.total_questions}")
    print()
    print("  Category breakdown:")
    for cat, acc in sorted(result.category_accuracy.items()):
        print(f"    {cat:<20} {acc:.1%}")
    print()
    m = result.eval_result.metrics
    print("  Retrieval metrics:")
    print(f"    Precision:       {m.recall_precision:.4f}")
    print(f"    Hit rate:        {m.recall_hit_rate:.4f}")
    print(f"    MRR:             {m.recall_mrr:.4f}")
    print(f"    NDCG:            {m.recall_ndcg:.4f}")
    print()
    print("  Latency:")
    print(f"    Retain p50:      {m.retain_latency_p50_ms:.1f} ms")
    print(f"    Retain p95:      {m.retain_latency_p95_ms:.1f} ms")
    print(f"    Recall p50:      {m.recall_latency_p50_ms:.1f} ms")
    print(f"    Recall p95:      {m.recall_latency_p95_ms:.1f} ms")
    print()
    print("  Cost:")
    print(f"    Tokens used:     {m.total_tokens_used}")
    print()
    print(f"  Total duration:    {m.total_duration_seconds:.1f}s")
    print(f"{'=' * 60}")


async def run_longmemeval(
    brain, data_path: str | None, max_questions: int | None,
    *, use_canonical_judge: bool = False, system: str = "astrocyte",
    max_sessions: int | None = None,
    checkpoint_dir: Path | None = None,
    resume: bool = False,
    retain_concurrency: int = 10,
    retain_rpm: int = 500,
    retain_tpm: int = 200_000,
    eval_concurrency: int = 5,
    eval_rpm: int = 500,
    eval_tpm: int = 200_000,
) -> BenchmarkRunOutcome:
    """Run LongMemEval benchmark."""
    from astrocyte.eval.benchmarks.longmemeval import (
        LongMemEvalBenchmark,
        LongMemEvalQuestion,
    )
    from astrocyte.eval.checkpoint import checkpoint_dir_for, load_or_create

    bench = LongMemEvalBenchmark(brain)

    dp = Path(data_path) if data_path else None
    if dp is None:
        has_dataset = False
    elif dp.is_file():
        has_dataset = True
    elif dp.is_dir():
        has_dataset = any(dp.glob("*.json"))
    else:
        has_dataset = False

    cp_dir = checkpoint_dir or checkpoint_dir_for(Path("benchmark-results"))
    is_resumable = _pipeline_is_persistent(brain)
    cp = load_or_create(
        "longmemeval", "bench-longmemeval", cp_dir,
        resume=resume, is_resumable=is_resumable,
    )
    if resume and not is_resumable:
        print("  [LongMemEval] WARNING: in-memory store — retain phase will re-run (data was lost on exit).")

    if has_dataset:
        result = await bench.run(
            data_path=data_path,
            bank_id="bench-longmemeval",
            max_questions=max_questions,
            max_sessions=max_sessions,
            use_canonical_judge=use_canonical_judge,
            checkpoint=cp,
            retain_concurrency=retain_concurrency,
            retain_rpm=retain_rpm,
            retain_tpm=retain_tpm,
            eval_concurrency=eval_concurrency,
            eval_rpm=eval_rpm,
            eval_tpm=eval_tpm,
        )
    else:
        if data_path:
            print(f"  WARNING: No JSON files found in {data_path}, using synthetic data.")
        # Synthetic smoke test when no dataset is available
        questions = [
            LongMemEvalQuestion(
                question_id="synth-1",
                category="extraction",
                question="What is Alice's favorite color?",
                answer="blue",
                session_ids=["s1"],
                conversation_context=[
                    {"content": "My favorite color is blue.", "role": "user", "session_id": "s1"},
                ],
            ),
            LongMemEvalQuestion(
                question_id="synth-2",
                category="temporal",
                question="When did Bob start his new job?",
                answer="March 2025",
                session_ids=["s2"],
                conversation_context=[
                    {"content": "I started my new job in March 2025.", "role": "user", "session_id": "s2"},
                ],
            ),
            LongMemEvalQuestion(
                question_id="synth-3",
                category="reasoning",
                question="Why does Carol prefer Python?",
                answer="readability",
                session_ids=["s3"],
                conversation_context=[
                    {
                        "content": "I prefer Python because of its readability and clean syntax.",
                        "role": "user",
                        "session_id": "s3",
                    },
                ],
            ),
        ]
        result = await bench.run(
            questions=questions,
            bank_id="bench-longmemeval",
            max_questions=max_questions,
            max_sessions=max_sessions,
            use_canonical_judge=use_canonical_judge,
            checkpoint=cp,
            retain_concurrency=retain_concurrency,
            retain_rpm=retain_rpm,
            retain_tpm=retain_tpm,
            eval_concurrency=eval_concurrency,
            eval_rpm=eval_rpm,
            eval_tpm=eval_tpm,
        )

    _print_result(result, "LongMemEval")
    return BenchmarkRunOutcome(
        _serialize_result(
            result, "longmemeval",
            judge="canonical" if use_canonical_judge else "legacy",
            system=system,
        ),
        has_dataset,
    )


def _pipeline_is_persistent(brain) -> bool:
    """Return True when the pipeline's vector store is persistent (not in-memory).

    Used by the checkpoint resume logic: with a persistent store, retained
    sessions survive a process exit so the retain phase can be skipped on
    resume. With an in-memory store, data is lost and retain must re-run.
    """
    try:
        from astrocyte.testing.in_memory import InMemoryVectorStore

        pipeline = getattr(brain, "_pipeline", None)
        if pipeline is None:
            return False
        vs = getattr(pipeline, "vector_store", None)
        return not isinstance(vs, InMemoryVectorStore)
    except ImportError:
        return True  # InMemoryVectorStore not present → real provider


def _locomo_llm_judge(brain, *, use_canonical_judge: bool):
    """Return a LoCoMoLLMJudge when conditions are right, else None.

    The LLM judge is only used when:
    - ``use_canonical_judge`` is True (caller opted in)
    - The pipeline has a real LLM provider (not MockLLMProvider)

    MockLLMProvider returns bag-of-words text that can't answer yes/no
    reliably, so LLM-judge scores with a mock provider are meaningless.
    We fall back to stemmed-F1 in that case so `bench-smoke` still works.
    """
    if not use_canonical_judge:
        return None
    pipeline = getattr(brain, "_pipeline", None)
    if pipeline is None:
        return None
    llm_provider = getattr(pipeline, "llm_provider", None)
    if llm_provider is None:
        return None
    # Skip if this is the mock (in-memory) provider used by bench-smoke.
    try:
        from astrocyte.testing.in_memory import MockLLMProvider

        if isinstance(llm_provider, MockLLMProvider):
            return None
    except ImportError:
        pass  # MockLLMProvider only exists in the test/dev extras; absent in prod
    from astrocyte.eval.judges.locomo_judge import LoCoMoLLMJudge

    return LoCoMoLLMJudge(llm_provider)


async def run_locomo(
    brain, data_path: str | None, max_questions: int | None,
    *, use_canonical_judge: bool = False, system: str = "astrocyte",
    checkpoint_dir: Path | None = None,
    resume: bool = False,
    eval_concurrency: int = 5,
    eval_rpm: int = 500,
    eval_tpm: int = 200_000,
) -> BenchmarkRunOutcome:
    """Run LoCoMo benchmark."""
    from astrocyte.eval.benchmarks.locomo import (
        LoComoBenchmark,
        LoCoMoConversation,
        LoCoMoQuestion,
        LoCoMoSession,
    )
    from astrocyte.eval.checkpoint import checkpoint_dir_for, load_or_create

    bench = LoComoBenchmark(brain)

    dp = Path(data_path) if data_path else None
    if dp is None:
        has_dataset = False
    elif dp.is_file():
        has_dataset = True
    elif dp.is_dir():
        has_dataset = bool(list(dp.glob("locomo*.json")) or list(dp.glob("*.json")))
    else:
        has_dataset = False

    cp_dir = checkpoint_dir or checkpoint_dir_for(Path("benchmark-results"))
    is_resumable = _pipeline_is_persistent(brain)
    cp = load_or_create(
        "locomo", "bench-locomo", cp_dir,
        resume=resume, is_resumable=is_resumable,
    )
    if resume and not is_resumable:
        print("  [LoCoMo] WARNING: in-memory store — retain phase will re-run (data was lost on exit).")

    llm_judge = _locomo_llm_judge(brain, use_canonical_judge=use_canonical_judge)
    if has_dataset:
        result = await bench.run(
            data_path=data_path,
            bank_id="bench-locomo",
            max_questions=max_questions,
            use_canonical_judge=use_canonical_judge,
            llm_judge=llm_judge,
            checkpoint=cp,
            eval_concurrency=eval_concurrency,
            eval_rpm=eval_rpm,
            eval_tpm=eval_tpm,
        )
    else:
        if data_path:
            print(f"  WARNING: dataset not found at {data_path}, using synthetic data.")
        # Synthetic smoke test when no dataset is available
        conversations = [
            LoCoMoConversation(
                conversation_id="synth-convo-1",
                sessions=[
                    LoCoMoSession(
                        session_id="session_1",
                        turns=[
                            {"speaker": "User1", "text": "I just moved to San Francisco last week."},
                            {"speaker": "User2", "text": "That's great! I've been living in NYC for 5 years."},
                        ],
                        date_time="January 15, 2025",
                    ),
                    LoCoMoSession(
                        session_id="session_2",
                        turns=[
                            {"speaker": "User1", "text": "I got a new job at a startup working on AI."},
                            {"speaker": "User2", "text": "Congrats! I'm still at the bank doing data analysis."},
                        ],
                        date_time="February 1, 2025",
                    ),
                    LoCoMoSession(
                        session_id="session_3",
                        turns=[
                            {"speaker": "User1", "text": "I adopted a golden retriever puppy named Max."},
                            {"speaker": "User2", "text": "That's adorable! I have two cats, Luna and Shadow."},
                        ],
                        date_time="March 10, 2025",
                    ),
                    LoCoMoSession(
                        session_id="session_4",
                        turns=[
                            {"speaker": "User1", "text": "Max is growing so fast! He loves the dog park near Golden Gate."},
                            {"speaker": "User2", "text": "I'm thinking of visiting SF next month. We should meet up!"},
                        ],
                        date_time="April 5, 2025",
                    ),
                ],
                questions=[
                    LoCoMoQuestion(
                        question="Where does User1 live?",
                        answer="San Francisco",
                        category="single-hop",
                        evidence_ids=["session_1"],
                        conversation_id="synth-convo-1",
                    ),
                    LoCoMoQuestion(
                        question="What does User2 do for work?",
                        answer="data analysis at a bank",
                        category="single-hop",
                        evidence_ids=["session_2"],
                        conversation_id="synth-convo-1",
                    ),
                    LoCoMoQuestion(
                        question="What is the name of User1's dog?",
                        answer="Max golden retriever",
                        category="single-hop",
                        evidence_ids=["session_3"],
                        conversation_id="synth-convo-1",
                    ),
                    LoCoMoQuestion(
                        question="What pets does User2 have?",
                        answer="two cats named Luna and Shadow",
                        category="single-hop",
                        evidence_ids=["session_3"],
                        conversation_id="synth-convo-1",
                    ),
                    LoCoMoQuestion(
                        question="Who changed cities recently and what is their new job?",
                        answer="User1 moved to San Francisco and works at an AI startup",
                        category="multi-hop",
                        evidence_ids=["session_1", "session_2"],
                        conversation_id="synth-convo-1",
                    ),
                    LoCoMoQuestion(
                        question="What park does User1 take Max to and in which city?",
                        answer="Golden Gate dog park in San Francisco",
                        category="multi-hop",
                        evidence_ids=["session_1", "session_4"],
                        conversation_id="synth-convo-1",
                    ),
                    LoCoMoQuestion(
                        question="When did User1 get their pet?",
                        answer="March 2025",
                        category="temporal",
                        evidence_ids=["session_3"],
                        conversation_id="synth-convo-1",
                    ),
                    LoCoMoQuestion(
                        question="Did User1 get their job before or after getting their dog?",
                        answer="before User1 got the job in February and the dog in March",
                        category="temporal",
                        evidence_ids=["session_2", "session_3"],
                        conversation_id="synth-convo-1",
                    ),
                ],
            )
        ]
        result = await bench.run(
            conversations=conversations,
            bank_id="bench-locomo",
            max_questions=max_questions,
            use_canonical_judge=use_canonical_judge,
            llm_judge=llm_judge,
            checkpoint=cp,
            eval_concurrency=eval_concurrency,
            eval_rpm=eval_rpm,
            eval_tpm=eval_tpm,
        )

    if llm_judge is not None:
        judge_label = "canonical-llm"
    elif use_canonical_judge:
        judge_label = "canonical"
    else:
        judge_label = "legacy"

    _print_result(result, "LoCoMo")
    return BenchmarkRunOutcome(
        _serialize_result(result, "locomo", judge=judge_label, system=system),
        has_dataset,
    )


async def run_builtin_suites(brain) -> dict:
    """Run built-in evaluation suites (basic + accuracy)."""
    from astrocyte.eval.evaluator import MemoryEvaluator

    evaluator = MemoryEvaluator(brain)
    results = {}

    for suite_name in ("basic", "accuracy"):
        result = await evaluator.run_suite(suite_name, bank_id=f"bench-{suite_name}")
        m = result.metrics

        print(f"\n{'=' * 60}")
        print(f"  Built-in suite: {suite_name}")
        print(f"{'=' * 60}")
        print(f"  Precision:       {m.recall_precision:.4f}")
        print(f"  Hit rate:        {m.recall_hit_rate:.4f}")
        print(f"  MRR:             {m.recall_mrr:.4f}")
        print(f"  NDCG:            {m.recall_ndcg:.4f}")
        if m.reflect_accuracy is not None:
            print(f"  Reflect acc:     {m.reflect_accuracy:.4f}")
        print(f"  Duration:        {m.total_duration_seconds:.1f}s")
        print(f"{'=' * 60}")

        results[suite_name] = {
            "suite": suite_name,
            "timestamp": result.timestamp.isoformat(),
            "metrics": {
                "recall_precision": m.recall_precision,
                "recall_hit_rate": m.recall_hit_rate,
                "recall_mrr": m.recall_mrr,
                "recall_ndcg": m.recall_ndcg,
                "reflect_accuracy": m.reflect_accuracy,
                "retain_latency_p50_ms": m.retain_latency_p50_ms,
                "retain_latency_p95_ms": m.retain_latency_p95_ms,
                "recall_latency_p50_ms": m.recall_latency_p50_ms,
                "recall_latency_p95_ms": m.recall_latency_p95_ms,
                "total_duration_seconds": m.total_duration_seconds,
            },
            "provider": result.provider,
            "provider_tier": result.provider_tier,
        }

    return results


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run Astrocyte memory benchmarks")
    parser.add_argument(
        "--config",
        help="Path to Astrocyte YAML config (for real providers)",
    )
    parser.add_argument(
        "--provider",
        default=None,
        choices=["test"],
        help="Use built-in test provider (no API keys needed)",
    )
    parser.add_argument(
        "--benchmarks",
        nargs="+",
        default=["builtin", "longmemeval", "locomo"],
        choices=["builtin", "longmemeval", "locomo"],
        help="Which benchmarks to run (default: all)",
    )
    parser.add_argument(
        "--longmemeval-path",
        help="Path to LongMemEval data directory",
    )
    parser.add_argument(
        "--locomo-path",
        help="Path to LoCoMo data directory or JSON file",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=None,
        help="Limit number of questions per benchmark (for quick testing)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for results JSON (default: benchmark-results/)",
    )
    parser.add_argument(
        "--canonical-judge",
        action="store_true",
        help=(
            "Score with each benchmark's canonical judge instead of the "
            "legacy word/text-overlap scorer. LoCoMo uses stemmed token "
            "F1 (astrocyte.eval.judges.locomo_judge); LongMemEval uses "
            "the paper's LLM-judge (one extra LLM call per question). "
            "REQUIRED for scores comparable to published numbers (paper, "
            "Mem0, Zep, Hindsight). Legacy scorer kept for internal "
            "delta-tracking."
        ),
    )
    parser.add_argument(
        "--multi-query",
        action="store_true",
        default=False,
        help=(
            "Enable multi-query expansion in the retrieval pipeline "
            "(PipelineOrchestrator.enable_multi_query_expansion). Rewrites "
            "each recall query into multiple sub-queries before retrieval, "
            "which improves recall on complex multi-hop questions at the "
            "cost of extra LLM calls. Off by default."
        ),
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=None,
        help=(
            "Cap LongMemEval retain phase at this many unique sessions. "
            "The full dataset has ~1500 sessions; 300-400 covers most "
            "evidence while halving retain cost. Default: retain all."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help=(
            "Resume an interrupted benchmark run from a checkpoint file in "
            "benchmark-results/checkpoints/. Already-retained sessions are "
            "skipped (with persistent stores only) and already-evaluated "
            "questions reuse their cached scores, saving LLM calls. "
            "No-op if no checkpoint exists (starts fresh)."
        ),
    )
    parser.add_argument(
        "--eval-concurrency",
        type=int,
        default=5,
        help="Max concurrent eval questions (default: 5). Tune down if hitting 429s.",
    )
    parser.add_argument(
        "--eval-rpm",
        type=int,
        default=500,
        help="Requests-per-minute budget for the eval rate limiter (default: 500).",
    )
    parser.add_argument(
        "--eval-tpm",
        type=int,
        default=200_000,
        help="Tokens-per-minute budget for the eval rate limiter (default: 200000).",
    )
    parser.add_argument(
        "--retain-concurrency",
        type=int,
        default=10,
        help="Max concurrent retain calls during LME retain phase (default: 10).",
    )
    parser.add_argument(
        "--retain-rpm",
        type=int,
        default=500,
        help="Requests-per-minute budget for the retain rate limiter (default: 500).",
    )
    parser.add_argument(
        "--retain-tpm",
        type=int,
        default=200_000,
        help="Tokens-per-minute budget for the retain rate limiter (default: 200000).",
    )
    args = parser.parse_args()

    # Build brain
    if args.provider == "test" or (not args.config and not args.provider):
        if not args.config:
            print("No --config provided, using in-memory test provider.")
        brain = _build_test_brain(enable_multi_query_expansion=args.multi_query)
    else:
        brain = _build_pipeline_brain(args.config, enable_multi_query_expansion=args.multi_query)

    # Run benchmarks
    all_results: dict = {}
    used_real_data: dict[str, bool] = {}
    wall_start = time.monotonic()

    if "builtin" in args.benchmarks:
        all_results["builtin"] = await run_builtin_suites(brain)

    # Run longmemeval + locomo concurrently when both are requested — they use
    # separate bank IDs and are fully independent. Single-benchmark runs stay
    # sequential so progress output isn't interleaved.
    run_lme = "longmemeval" in args.benchmarks
    run_loc = "locomo" in args.benchmarks

    cp_dir = Path(args.output_dir or "benchmark-results") / "checkpoints"

    if run_lme and run_loc:
        lme_outcome, loc_outcome = await asyncio.gather(
            run_longmemeval(
                brain, args.longmemeval_path, args.max_questions,
                use_canonical_judge=args.canonical_judge,
                max_sessions=args.max_sessions,
                checkpoint_dir=cp_dir,
                resume=args.resume,
                retain_concurrency=args.retain_concurrency,
                retain_rpm=args.retain_rpm,
                retain_tpm=args.retain_tpm,
                eval_concurrency=args.eval_concurrency,
                eval_rpm=args.eval_rpm,
                eval_tpm=args.eval_tpm,
            ),
            run_locomo(
                brain, args.locomo_path, args.max_questions,
                use_canonical_judge=args.canonical_judge,
                checkpoint_dir=cp_dir,
                resume=args.resume,
                eval_concurrency=args.eval_concurrency,
                eval_rpm=args.eval_rpm,
                eval_tpm=args.eval_tpm,
            ),
        )
        if lme_outcome.result:
            all_results["longmemeval"] = lme_outcome.result
        used_real_data["longmemeval"] = lme_outcome.used_real_data
        if loc_outcome.result:
            all_results["locomo"] = loc_outcome.result
        used_real_data["locomo"] = loc_outcome.used_real_data
    else:
        if run_lme:
            outcome = await run_longmemeval(
                brain, args.longmemeval_path, args.max_questions,
                use_canonical_judge=args.canonical_judge,
                max_sessions=args.max_sessions,
                checkpoint_dir=cp_dir,
                resume=args.resume,
                retain_concurrency=args.retain_concurrency,
                retain_rpm=args.retain_rpm,
                retain_tpm=args.retain_tpm,
                eval_concurrency=args.eval_concurrency,
                eval_rpm=args.eval_rpm,
                eval_tpm=args.eval_tpm,
            )
            if outcome.result:
                all_results["longmemeval"] = outcome.result
            used_real_data["longmemeval"] = outcome.used_real_data

        if run_loc:
            outcome = await run_locomo(
                brain, args.locomo_path, args.max_questions,
                use_canonical_judge=args.canonical_judge,
                checkpoint_dir=cp_dir,
                resume=args.resume,
                eval_concurrency=args.eval_concurrency,
                eval_rpm=args.eval_rpm,
                eval_tpm=args.eval_tpm,
            )
            if outcome.result:
                all_results["locomo"] = outcome.result
            used_real_data["locomo"] = outcome.used_real_data

    wall_elapsed = time.monotonic() - wall_start

    # Summary
    print(f"\n{'#' * 60}")
    print(f"  All benchmarks complete in {wall_elapsed:.1f}s")
    print(f"{'#' * 60}")

    # Write results
    output_dir = Path(args.output_dir or "benchmark-results")
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_file = output_dir / f"results-{timestamp}.json"

    all_results["_meta"] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "wall_time_seconds": wall_elapsed,
        "provider": brain._provider_name,
        "provider_tier": brain._config.provider_tier,
        "max_questions": args.max_questions,
        "config": args.config,
    }

    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    print(f"\n  Results written to {output_file}")

    # Also write a latest.json symlink/copy for easy CI access
    latest = output_dir / "latest.json"
    with open(latest, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    # Exit non-zero if a real dataset benchmark had 0% accuracy (likely broken).
    # Only check benchmarks that actually loaded real data (synthetic tests may
    # legitimately score 0% with mock providers).
    if any(used_real_data.values()):
        for key in ("longmemeval", "locomo"):
            if not used_real_data.get(key):
                continue
            if key in all_results and all_results[key].get("overall_accuracy", 1.0) == 0.0:
                print(f"\n  WARNING: {key} had 0% accuracy — something may be wrong.")
                sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
