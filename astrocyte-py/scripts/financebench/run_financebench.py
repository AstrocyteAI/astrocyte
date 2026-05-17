"""FinanceBench harness — Document Engine bench.

Runs Astrocyte's Document Engine against the FinanceBench open-source
dataset (patronus-ai/financebench, ~150 questions over 10-K SEC filings).

Two retrieval strategies, controlled by --strategy:

  vector       PDF → tree → Memory Engine retain → memory.recall() (vector)
               Baseline. Runs today with pymupdf PDF parsing.

  tree_search  Same ingest + DocumentStore → DocumentNavigator.search()
               Requires Phase A (MarkitdownParser) + Phase C/D (DocumentNavigator).

Usage:
    uv run python scripts/financebench/run_financebench.py \\
        --dataset-dir datasets/financebench \\
        --strategy vector \\
        --project financebench-vector-baseline

    # or via Makefile:
    make bench-financebench FINANCE_STRATEGY=vector
    make bench-financebench FINANCE_STRATEGY=tree_search
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.financebench._client import FinanceBenchClient
from scripts.financebench._dataset import (
    FinanceBenchEntry,
    load_dataset,
    pdf_path,
    unique_docs,
)
from scripts.financebench._scoring import judge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("financebench")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="FinanceBench Document Engine harness")
    p.add_argument(
        "--dataset-dir",
        required=True,
        type=Path,
        help="Root of the cloned patronus-ai/financebench repo.",
    )
    p.add_argument(
        "--strategy",
        choices=["vector", "tree_search"],
        default="vector",
        help="Retrieval strategy: vector (baseline) or tree_search (Phase C/D).",
    )
    p.add_argument(
        "--project",
        default="financebench",
        help="Label written into the results JSON filename.",
    )
    p.add_argument(
        "--max-questions",
        type=int,
        default=0,
        help="Cap on questions to run (0 = all).",
    )
    p.add_argument(
        "--bank-id",
        default="bench-financebench",
        help="Astrocyte bank_id for this bench run.",
    )
    p.add_argument(
        "--answerer-model",
        default=os.environ.get("FINANCE_ANSWERER_MODEL", "gpt-4o-mini"),
    )
    p.add_argument(
        "--judge-model",
        default=os.environ.get("FINANCE_JUDGE_MODEL", "gpt-4o-mini"),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmark-results/financebench"),
        help="Directory where results JSON is written.",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(args: argparse.Namespace) -> None:
    dataset_dir = args.dataset_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load dataset
    entries = load_dataset(dataset_dir, max_questions=args.max_questions)
    docs = unique_docs(entries)
    logger.info(
        "Loaded %d questions across %d documents (strategy=%s)",
        len(entries),
        len(docs),
        args.strategy,
    )

    results: list[dict] = []
    n_correct = 0
    run_start = time.monotonic()

    async with FinanceBenchClient(
        strategy=args.strategy,
        bank_id=args.bank_id,
        answerer_model=args.answerer_model,
        judge_model=args.judge_model,
    ) as client:

        # ── Ingest all documents upfront ──────────────────────────────
        logger.info("Ingesting %d PDFs...", len(docs))
        for doc_name in docs:
            path = pdf_path(dataset_dir, doc_name)
            if not path.exists():
                logger.warning("PDF not found, skipping: %s", path)
                continue
            try:
                await client.ingest_pdf(path, doc_name)
            except Exception as exc:  # noqa: BLE001
                logger.error("ingest failed for %s: %s", doc_name, exc)

        logger.info("Ingest complete. Running %d questions...", len(entries))

        # ── Question loop ─────────────────────────────────────────────
        for i, entry in enumerate(entries, start=1):
            q_start = time.monotonic()

            if entry.doc_name not in client._doc_ids:
                logger.warning(
                    "[%d/%d] %s — PDF was not ingested, skipping",
                    i, len(entries), entry.financebench_id,
                )
                results.append(_make_result(entry, "", "", False, skipped=True))
                continue

            try:
                context = await client.retrieve(entry.question, entry.doc_name)
                model_answer = await client.answer(entry.question, context)
                correct = await judge(
                    entry.question,
                    entry.answer,
                    model_answer,
                    client.judge_llm_call(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[%d/%d] %s — error: %s",
                    i, len(entries), entry.financebench_id, exc,
                )
                results.append(_make_result(entry, "", str(exc), False, error=str(exc)))
                continue

            if correct:
                n_correct += 1
            q_elapsed = time.monotonic() - q_start

            logger.info(
                "[%d/%d] %s  %s  (%.1fs)",
                i, len(entries),
                entry.financebench_id,
                "✓" if correct else "✗",
                q_elapsed,
            )
            results.append(_make_result(entry, context, model_answer, correct))

        # ── Summary ───────────────────────────────────────────────────
        answered = sum(1 for r in results if not r.get("skipped") and not r.get("error"))
        accuracy = n_correct / answered if answered else 0.0
        total_elapsed = time.monotonic() - run_start

        summary = {
            "strategy": args.strategy,
            "project": args.project,
            "answerer_model": args.answerer_model,
            "judge_model": args.judge_model,
            "bank_id": args.bank_id,
            "total_questions": len(entries),
            "answered": answered,
            "correct": n_correct,
            "accuracy": round(accuracy, 4),
            "accuracy_pct": round(accuracy * 100, 2),
            "elapsed_s": round(total_elapsed, 1),
            "run_at": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(
            "Result: %d/%d correct = %.1f%% (%s strategy, %.0fs)",
            n_correct, answered, accuracy * 100, args.strategy, total_elapsed,
        )

        # ── Write results JSON ────────────────────────────────────────
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = output_dir / f"financebench_results_{ts}.json"
        out_path.write_text(
            json.dumps({"summary": summary, "results": results}, indent=2)
        )
        logger.info("Results written to %s", out_path)


def _make_result(
    entry: FinanceBenchEntry,
    context: str,
    model_answer: str,
    correct: bool,
    *,
    skipped: bool = False,
    error: str | None = None,
) -> dict:
    return {
        "financebench_id": entry.financebench_id,
        "doc_name": entry.doc_name,
        "question": entry.question,
        "ground_truth": entry.answer,
        "model_answer": model_answer,
        "correct": correct,
        "question_type": entry.question_type,
        "domain": entry.domain,
        "page_number": entry.page_number,
        **({"skipped": True} if skipped else {}),
        **({"error": error} if error else {}),
    }


if __name__ == "__main__":
    asyncio.run(_run(_parse_args()))
