#!/usr/bin/env python3
"""Promote a benchmark-results file into a committed baseline.

Two flows:

1. Snapshot a run into ``benchmarks/snapshots/{bench}-{tag}.json``
   (historical record; tracked in git). Use descriptive tags like
   ``v3-real-openai`` or ``2026-05-01-consolidation``.

2. Promote a run into ``benchmarks/baselines-{suite}.json``
   (active CI regression floor). Writes the summary shape the
   regression gate expects (no per-question details).

Separation matches the convention documented in
``benchmarks/snapshots/README.md`` — snapshots are the changelog,
baselines are the threshold CI compares against.

Usage:
    # Snapshot the most recent run (historical record)
    python scripts/promote_benchmark_baseline.py snapshot \\
        --bench locomo --tag v3-real-openai

    # Explicit results file
    python scripts/promote_benchmark_baseline.py snapshot \\
        --bench longmemeval --tag v3-real-openai \\
        --results benchmark-results/results-20260421T184547Z.json

    # Promote to the active OpenAI baseline (CI floor)
    python scripts/promote_benchmark_baseline.py promote \\
        --suite openai \\
        --results benchmark-results/results-20260421T184547Z.json
"""

from __future__ import annotations

import argparse
import glob
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS = REPO_ROOT / "benchmarks" / "snapshots"


def _most_recent_run() -> Path:
    matches = sorted(
        glob.glob(str(REPO_ROOT / "benchmark-results" / "results-*.json")),
    )
    if not matches:
        print("error: no results-*.json files found in benchmark-results/",
              file=sys.stderr)
        sys.exit(2)
    return Path(matches[-1])


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    with open(path) as f:
        return json.load(f)


def _baseline_shape(bench: dict[str, Any], notes: str) -> dict[str, Any]:
    """Reduce a full benchmark run to the shape the regression gate reads.

    Drops ``per_question`` and other verbose fields; keeps overall,
    category breakdown, and retrieval metrics. All numbers are rounded
    to 4 decimal places so diffs are readable.
    """
    return {
        "overall_accuracy": round(float(bench.get("overall_accuracy", 0.0)), 4),
        "category_accuracy": {
            k: round(float(v), 4)
            for k, v in (bench.get("category_accuracy") or {}).items()
        },
        "metrics": {
            k: round(float(v), 4)
            for k, v in (bench.get("metrics") or {}).items()
            if v is not None
        },
        "notes": notes,
    }


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Copy a results file into benchmarks/snapshots/ under a named tag."""
    results_path = Path(args.results) if args.results else _most_recent_run()
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    dest = SNAPSHOTS / f"{args.bench}-{args.tag}.json"
    if dest.exists() and not args.force:
        print(f"error: {dest} already exists (pass --force to overwrite)",
              file=sys.stderr)
        return 1
    shutil.copy(results_path, dest)
    print(f"snapshotted: {results_path.name} -> {dest.relative_to(REPO_ROOT)}")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """Write/update benchmarks/baselines-{suite}.json with summary numbers."""
    results_path = Path(args.results) if args.results else _most_recent_run()
    results = _load(results_path)

    baseline_path = REPO_ROOT / "benchmarks" / f"baselines-{args.suite}.json"
    existing = _load(baseline_path) if baseline_path.exists() else {}

    notes = args.notes or (
        f"Promoted from {results_path.name}. Suite: {args.suite}. "
        "Real-provider runs are noisier than mock; consider looser "
        "tolerances in CI (e.g. --overall-tolerance 0.04)."
    )

    for bench_key in ("locomo", "longmemeval"):
        bench = results.get(bench_key)
        if not isinstance(bench, dict) or "overall_accuracy" not in bench:
            continue  # not present in this results file
        existing[bench_key] = {
            "provider": args.suite,
            **_baseline_shape(bench, notes),
        }

    if not existing:
        print("error: no benchmark sections found in results — nothing to promote",
              file=sys.stderr)
        return 1

    with open(baseline_path, "w") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")
    print(f"promoted: {results_path.name} -> {baseline_path.relative_to(REPO_ROOT)}")
    print(json.dumps(existing, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    snap = sub.add_parser("snapshot", help="Copy a run into benchmarks/snapshots/")
    snap.add_argument("--bench", required=True, choices=["locomo", "longmemeval"])
    snap.add_argument("--tag", required=True,
                      help="Version tag (e.g. v3-real-openai)")
    snap.add_argument("--results", default=None,
                      help="Path to results JSON (default: most recent)")
    snap.add_argument("--force", action="store_true",
                      help="Overwrite an existing snapshot file")
    snap.set_defaults(func=cmd_snapshot)

    prom = sub.add_parser("promote",
                          help="Update benchmarks/baselines-{suite}.json (CI floor)")
    prom.add_argument("--suite", required=True,
                      help="Baseline suite name (e.g. openai, test-provider)")
    prom.add_argument("--results", default=None,
                      help="Path to results JSON (default: most recent)")
    prom.add_argument("--notes", default=None,
                      help="Custom notes text (default: auto-generated)")
    prom.set_defaults(func=cmd_promote)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
