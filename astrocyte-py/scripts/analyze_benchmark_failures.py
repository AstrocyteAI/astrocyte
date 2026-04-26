#!/usr/bin/env python3
"""Analyze serialized benchmark failures and emit actionable buckets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from astrocyte.eval.failure_analysis import (
    analyze_failures,
    load_benchmark_result,
    stable_question_slice,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_json", type=Path)
    parser.add_argument("--slice-size", type=int, default=200)
    parser.add_argument("--seed", default="locomo-v1")
    args = parser.parse_args()

    result = load_benchmark_result(args.result_json)
    analysis = analyze_failures(result)
    analysis["stable_question_slice"] = stable_question_slice(
        result,
        size=args.slice_size,
        seed=args.seed,
    )
    print(json.dumps(analysis, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
