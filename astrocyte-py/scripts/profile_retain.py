"""Profile the retain pipeline at small scale to identify hot stages.

Why this exists: the LME bench observed per-session retain wall time
drift from ~0.83s → ~2.6s as the corpus grew. Initial hypothesis was
HNSW concurrent-insert lock contention — switching to pgvectorscale's
DiskANN didn't help, and a 4-way deadlock cycle on advisory locks
later showed AGE entity-merge as a likely real bottleneck.

This script settles the question with evidence rather than more
guessing. It:

1. Boots Astrocyte against the bench Postgres on :5433.
2. Loads ``--max-questions`` LME questions (default 50, so a bounded
   number of unique sessions — typically a few hundred).
3. Retains those sessions through ``Astrocyte.retain`` with the
   ``ASTROCYTE_RETAIN_PROFILE=1`` env enabled, so the orchestrator's
   ``_RetainProfiler`` captures per-stage wall time at every key call
   site (SFE, embed, store_vec, entity_emb, entity_resolve,
   entity_store, entity_link_mem, entity_co_occur).
4. Prints aggregated p50/p95/max per stage, ordered by total wall
   time. The dominant row is the bottleneck.

Usage:
  doppler run -- uv run --extra dev --extra rerank \\
      python scripts/profile_retain.py \\
      --config benchmarks/config-stacked-cheap-sfe-parallel.yaml \\
      --max-questions 50

Pre-reqs: ``make bench-db-start`` (port 5433); the script wipes the
schema on entry so prior bench state doesn't skew the timings.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

# Force-enable retain profiling before importing astrocyte — the
# _RetainProfiler reads the env var at construction time.
os.environ.setdefault("ASTROCYTE_RETAIN_PROFILE", "1")

from astrocyte.eval.benchmarks.longmemeval import (  # noqa: E402
    load_longmemeval_dataset,
)


async def _run(config_path: str, lme_path: str, max_questions: int) -> None:
    # Use the bench Postgres on :5433. Override DSNs so the script
    # doesn't accidentally hit a production deployment.
    dsn = "postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte_bench"
    os.environ["DATABASE_URL"] = dsn
    os.environ["ASTROCYTE_TASKS_DSN"] = dsn

    # Reuse the bench harness's brain construction to keep the profile
    # run identical to bench retain semantics (same providers, same
    # entity-resolver wiring, same SFE config). Importing the function
    # directly avoids duplicating ~150 lines of provider plumbing.
    from scripts.run_benchmarks import _build_pipeline_brain  # noqa: E402

    brain = _build_pipeline_brain(config_path)

    try:
        # Wipe any prior bench state so timings reflect a fresh corpus.
        # We don't care about preserving data; the entire purpose of
        # this script is profiling on a known-empty schema.
        from astrocyte.eval._state_reset import reset_benchmark_state

        await reset_benchmark_state()
    except Exception as exc:
        print(f"  [profile] reset_benchmark_state skipped: {exc}")

    # Load N questions; LME conversation_context groups messages by
    # session_id (each msg is one session in LME's data shape — same
    # convention as the bench harness in benchmarks/longmemeval.py).
    questions = load_longmemeval_dataset(lme_path, max_questions=max_questions)
    print(f"  [profile] loaded {len(questions)} LME questions")

    # Collect unique sessions, dedup by session_id across questions
    # (haystacks share sessions). Same logic as the bench harness so
    # the profile run hits the same retain code path.
    seen: set[str] = set()
    sessions: list[tuple[str, str, str]] = []  # (session_id, role, content)
    for q in questions:
        for msg in q.conversation_context:
            sid = msg.get("session_id", "")
            if not sid or sid in seen:
                continue
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            seen.add(sid)
            sessions.append((sid, msg.get("role", "user"), content))

    print(f"  [profile] retaining {len(sessions)} unique sessions")

    # Run retain calls sequentially so the profiler attributes timings
    # cleanly to each call site. Concurrent retains would still be
    # captured, but we'd need to disambiguate which call belongs to
    # which session — sequential is enough for finding the slow stage.
    bank_id = "lme-profile"
    t0 = time.monotonic()
    pipeline = brain._pipeline
    for i, (sid, role, content) in enumerate(sessions):
        await brain.retain(
            content,
            bank_id=bank_id,
            tags=[role, f"lme:session:{sid}"],
        )
        if (i + 1) % 25 == 0:
            elapsed = time.monotonic() - t0
            print(
                f"\n  [profile] retained {i + 1}/{len(sessions)} sessions "
                f"({elapsed:.1f}s, {elapsed / (i + 1):.3f}s/session avg)",
            )
            # Periodic breakdown so we see the bottleneck emerge mid-run
            # without waiting for full completion. Useful when the
            # retain wall is dominated by a single stage and we want
            # to bail early once the answer is obvious.
            if pipeline is not None and hasattr(pipeline, "_profiler"):
                pipeline._profiler.report(prefix=f"[profile@{i + 1}]")
    elapsed = time.monotonic() - t0
    print(
        f"\n  [profile] retained {len(sessions)} sessions in {elapsed:.1f}s "
        f"({elapsed / max(len(sessions), 1):.3f}s/session avg)",
    )

    # Print per-stage breakdown. The orchestrator's _RetainProfiler.report
    # logs a sorted-by-total table at INFO level.
    pipeline = brain._pipeline
    if pipeline is not None and hasattr(pipeline, "_profiler"):
        pipeline._profiler.report()
    else:
        print("  [profile] WARNING: no pipeline profiler attached (Tier 2 deployment?)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="Path to benchmark YAML config")
    ap.add_argument(
        "--lme-path",
        default="datasets/longmemeval/data",
        help="Directory containing longmemeval_s_cleaned.json",
    )
    ap.add_argument(
        "--max-questions",
        type=int,
        default=50,
        help="Number of LME questions to load (each has a haystack of ~40 sessions)",
    )
    args = ap.parse_args()

    if not Path(args.config).exists():
        raise SystemExit(f"config not found: {args.config}")

    # Make sure the env var is set so even if the env was sanitized
    # earlier, the orchestrator's profiler turns on.
    os.environ["ASTROCYTE_RETAIN_PROFILE"] = "1"

    # Logging setup: surface the orchestrator's INFO-level report().
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )

    asyncio.run(_run(args.config, args.lme_path, args.max_questions))


if __name__ == "__main__":
    main()
