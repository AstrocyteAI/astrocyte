#!/usr/bin/env python3
"""Check the latest benchmark run against a checked-in baseline.

Closes the L4 eval-harness gap described in
``docs/_design/platform-positioning.md`` by providing the regression
gate that turns the weekly benchmark run into a CI signal. Compares
``benchmark-results/latest.json`` (or any named results file) against a
baseline JSON, and exits non-zero when any metric drops by more than a
per-field threshold.

Usage:
    # Compare latest mock-provider run against the checked-in baseline.
    python scripts/check_benchmark_regression.py \\
        --baseline benchmarks/baselines-test-provider.json

    # Compare a specific results file.
    python scripts/check_benchmark_regression.py \\
        --results benchmark-results/results-20260421T175222Z.json \\
        --baseline benchmarks/baselines-test-provider.json

    # Looser thresholds for real-provider CI where LLM sampling creates noise.
    python scripts/check_benchmark_regression.py \\
        --baseline benchmarks/baselines-openai.json \\
        --overall-tolerance 0.03 --category-tolerance 0.05

Exit codes:
    0 — all metrics within tolerance
    1 — regression detected (stdout details which)
    2 — inputs malformed (baseline or results missing / not JSON)

Emits a GitHub Actions ``$GITHUB_STEP_SUMMARY`` block when that env var
is set, so the regression table appears directly in the run summary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON in {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def _overall(bench_result: dict[str, Any]) -> float | None:
    """Accept either top-level ``overall_accuracy`` or a ``builtin`` shape."""
    if "overall_accuracy" in bench_result:
        return float(bench_result["overall_accuracy"])
    return None


def _metric(bench_result: dict[str, Any], key: str) -> float | None:
    metrics = bench_result.get("metrics") or {}
    val = metrics.get(key)
    return float(val) if val is not None else None


def _category(bench_result: dict[str, Any], cat: str) -> float | None:
    cats = bench_result.get("category_accuracy") or {}
    val = cats.get(cat)
    return float(val) if val is not None else None


def _compare_field(
    label: str,
    baseline: float | None,
    actual: float | None,
    tolerance: float,
    regressions: list[str],
    rows: list[tuple[str, str, str, str, str]],
) -> None:
    """Compare a single metric and record outcome.

    ``rows`` accumulates Markdown-table rows for the GitHub summary.
    ``regressions`` accumulates human-readable lines for the console.
    """
    if baseline is None and actual is None:
        return
    if baseline is None:
        rows.append((label, "—", f"{actual:.4f}" if actual is not None else "—", "⚠ no baseline", ""))
        return
    if actual is None:
        rows.append((label, f"{baseline:.4f}", "—", "⚠ missing in run", ""))
        regressions.append(f"{label}: baseline {baseline:.4f} but no value in latest run")
        return
    delta = actual - baseline
    if delta < -tolerance:
        status = "❌ regression"
        regressions.append(
            f"{label}: baseline {baseline:.4f}, actual {actual:.4f} "
            f"(Δ {delta:+.4f}, tolerance {tolerance:.4f})"
        )
    elif delta < 0:
        status = "⚠ minor dip"
    elif delta > tolerance:
        status = "✅ improvement"
    else:
        status = "✅ stable"
    rows.append((label, f"{baseline:.4f}", f"{actual:.4f}", f"{delta:+.4f}", status))


def compare_benchmark(
    bench_name: str,
    baseline: dict[str, Any],
    actual: dict[str, Any],
    *,
    overall_tolerance: float,
    category_tolerance: float,
    metric_tolerance: float,
) -> tuple[list[str], list[tuple[str, str, str, str, str]]]:
    regressions: list[str] = []
    rows: list[tuple[str, str, str, str, str]] = []

    _compare_field(
        f"{bench_name}:overall",
        _overall(baseline),
        _overall(actual),
        overall_tolerance,
        regressions, rows,
    )

    # Compare every category in the baseline; the actual may have more
    # (new category added) but not fewer without flagging.
    for cat in (baseline.get("category_accuracy") or {}).keys():
        _compare_field(
            f"{bench_name}:{cat}",
            _category(baseline, cat),
            _category(actual, cat),
            category_tolerance,
            regressions, rows,
        )

    for metric_key in ("recall_hit_rate", "recall_mrr", "recall_precision"):
        _compare_field(
            f"{bench_name}:{metric_key}",
            _metric(baseline, metric_key),
            _metric(actual, metric_key),
            metric_tolerance,
            regressions, rows,
        )

    return regressions, rows


def _emit_github_summary(all_rows: list[tuple[str, str, str, str, str]], regressions: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Benchmark regression check", ""]
    if regressions:
        lines.append(f"**{len(regressions)} regression(s) detected.**")
        lines.append("")
    else:
        lines.append("**No regressions detected.**")
        lines.append("")
    lines.append("| Metric | Baseline | Actual | Δ | Status |")
    lines.append("|---|---|---|---|---|")
    for row in all_rows:
        lines.append("| " + " | ".join(row) + " |")
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--baseline", required=True, type=Path,
        help="Path to baselines JSON (e.g. benchmarks/baselines-test-provider.json)",
    )
    parser.add_argument(
        "--results", type=Path,
        default=Path("benchmark-results/latest.json"),
        help="Path to the latest results JSON (default: benchmark-results/latest.json)",
    )
    parser.add_argument(
        "--overall-tolerance", type=float, default=0.02,
        help="Max allowed drop on overall_accuracy before flagging (default: 0.02 = 2pp)",
    )
    parser.add_argument(
        "--category-tolerance", type=float, default=0.03,
        help="Max allowed drop on any category accuracy (default: 0.03 = 3pp)",
    )
    parser.add_argument(
        "--metric-tolerance", type=float, default=0.03,
        help="Max allowed drop on any retrieval metric (default: 0.03)",
    )
    args = parser.parse_args()

    baseline = _load_json(args.baseline)
    results = _load_json(args.results)

    all_regressions: list[str] = []
    all_rows: list[tuple[str, str, str, str, str]] = []

    for bench_name, bench_baseline in baseline.items():
        if not isinstance(bench_baseline, dict):
            continue
        actual = results.get(bench_name)
        if not isinstance(actual, dict):
            all_regressions.append(f"{bench_name}: present in baseline but missing from results")
            all_rows.append((f"{bench_name}:*", "see baseline", "—", "⚠", "missing in run"))
            continue
        regs, rows = compare_benchmark(
            bench_name, bench_baseline, actual,
            overall_tolerance=args.overall_tolerance,
            category_tolerance=args.category_tolerance,
            metric_tolerance=args.metric_tolerance,
        )
        all_regressions.extend(regs)
        all_rows.extend(rows)

    # Console report — always printed.
    print("Benchmark regression check")
    print(f"  baseline: {args.baseline}")
    print(f"  results:  {args.results}")
    print(f"  tolerances: overall={args.overall_tolerance} "
          f"category={args.category_tolerance} metric={args.metric_tolerance}")
    print()
    print(f"{'Metric':<40} {'Baseline':>10} {'Actual':>10} {'Delta':>10}  Status")
    print("-" * 90)
    for row in all_rows:
        print(f"{row[0]:<40} {row[1]:>10} {row[2]:>10} {row[3]:>10}  {row[4]}")
    print()
    if all_regressions:
        print(f"❌ {len(all_regressions)} regression(s):")
        for r in all_regressions:
            print(f"  - {r}")
    else:
        print("✅ No regressions detected.")

    _emit_github_summary(all_rows, all_regressions)

    return 1 if all_regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
