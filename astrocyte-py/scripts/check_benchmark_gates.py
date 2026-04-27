#!/usr/bin/env python3
"""Check benchmark results against release gates.

Regression checks compare against a previous baseline. Gates are different:
they define the minimum quality and maximum latency/cost required before a
capability claim is allowed.

Usage:
    python scripts/check_benchmark_gates.py \\
        --gates benchmarks/gates-hindsight-informed.json \\
        --results benchmark-results/latest.json
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
        raise SystemExit(2)
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON in {path}: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _get_path(data: dict[str, Any], dotted: str) -> float | None:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    try:
        return float(cur)
    except (TypeError, ValueError):
        return None


def check_gates(
    gates: dict[str, Any],
    results: dict[str, Any],
) -> tuple[list[str], list[tuple[str, str, str, str]]]:
    """Return failures and table rows for configured gates."""
    failures: list[str] = []
    rows: list[tuple[str, str, str, str]] = []

    for scenario, scenario_gates in gates.items():
        actual = results.get(scenario)
        if not isinstance(actual, dict):
            failures.append(f"{scenario}: missing from results")
            rows.append((scenario, "present", "missing", "fail"))
            continue

        minimums = scenario_gates.get("minimums") or {}
        for field, expected_raw in minimums.items():
            expected = float(expected_raw)
            got = _get_path(actual, field)
            label = f"{scenario}:{field}"
            if got is None:
                failures.append(f"{label}: missing")
                rows.append((label, f">= {expected:.4f}", "missing", "fail"))
            elif got < expected:
                failures.append(f"{label}: expected >= {expected:.4f}, got {got:.4f}")
                rows.append((label, f">= {expected:.4f}", f"{got:.4f}", "fail"))
            else:
                rows.append((label, f">= {expected:.4f}", f"{got:.4f}", "pass"))

        maximums = scenario_gates.get("maximums") or {}
        for field, expected_raw in maximums.items():
            expected = float(expected_raw)
            got = _get_path(actual, field)
            label = f"{scenario}:{field}"
            if got is None:
                failures.append(f"{label}: missing")
                rows.append((label, f"<= {expected:.4f}", "missing", "fail"))
            elif got > expected:
                failures.append(f"{label}: expected <= {expected:.4f}, got {got:.4f}")
                rows.append((label, f"<= {expected:.4f}", f"{got:.4f}", "fail"))
            else:
                rows.append((label, f"<= {expected:.4f}", f"{got:.4f}", "pass"))

    return failures, rows


def _emit_github_summary(rows: list[tuple[str, str, str, str]], failures: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Benchmark release gates", ""]
    lines.append("**Pass.**" if not failures else f"**{len(failures)} gate failure(s).**")
    lines.append("")
    lines.append("| Gate | Expected | Actual | Status |")
    lines.append("|---|---|---|---|")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gates", required=True, type=Path, help="Gate JSON file")
    parser.add_argument("--results", required=True, type=Path, help="Benchmark result JSON file")
    args = parser.parse_args()

    gates = _load_json(args.gates)
    results = _load_json(args.results)
    failures, rows = check_gates(gates, results)

    print("Benchmark release gates")
    print(f"  gates:   {args.gates}")
    print(f"  results: {args.results}")
    print()
    print(f"{'Gate':<55} {'Expected':>14} {'Actual':>14}  Status")
    print("-" * 95)
    for label, expected, got, status in rows:
        print(f"{label:<55} {expected:>14} {got:>14}  {status}")
    print()

    if failures:
        print(f"{len(failures)} gate failure(s):")
        for failure in failures:
            print(f"  - {failure}")
        _emit_github_summary(rows, failures)
        return 1

    print("All benchmark gates passed.")
    _emit_github_summary(rows, failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
