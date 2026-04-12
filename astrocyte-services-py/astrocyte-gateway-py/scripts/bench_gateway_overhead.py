#!/usr/bin/env python3
"""Measure gateway-only overhead: in-process ASGI HTTP vs direct ``brain.recall`` (same ``Astrocyte``).

Uses ``httpx.ASGITransport`` (no TCP; no uvicorn). Core work is in-memory + mock LLM by default
so the gap is mostly FastAPI + JSON + middleware + ``to_jsonable``.

Environment: set ``ASTROCYTE_AUTH_MODE=dev`` unless already set; clears ``ASTROCYTE_CONFIG_PATH``
so the benchmark uses the same default stack as local dev without a YAML file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from typing import Any


def _percentile_ns(sorted_ns: list[int], q: float) -> float:
    if not sorted_ns:
        return 0.0
    if len(sorted_ns) == 1:
        return float(sorted_ns[0])
    pos = (len(sorted_ns) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_ns) - 1)
    frac = pos - lo
    return sorted_ns[lo] * (1 - frac) + sorted_ns[hi] * frac


def _summarize(name: str, samples_ns: list[int]) -> dict[str, float]:
    s = sorted(samples_ns)
    return {
        f"{name}_p50_ms": _percentile_ns(s, 50) / 1e6,
        f"{name}_p95_ms": _percentile_ns(s, 95) / 1e6,
        f"{name}_p99_ms": _percentile_ns(s, 99) / 1e6,
        f"{name}_mean_ms": statistics.mean(samples_ns) / 1e6,
    }


async def _run(*, warmup: int, iterations: int) -> dict[str, Any]:
    os.environ.setdefault("ASTROCYTE_AUTH_MODE", "dev")
    os.environ.pop("ASTROCYTE_CONFIG_PATH", None)

    import httpx

    from astrocyte_gateway.app import create_app
    from astrocyte_gateway.brain import build_astrocyte

    brain = build_astrocyte()
    app = create_app(brain)

    payload: dict[str, Any] = {"query": "bench", "bank_id": "b1", "max_results": 5}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(warmup):
            await brain.recall(
                payload["query"],
                bank_id=payload["bank_id"],
                max_results=payload["max_results"],
                context=None,
            )
            r = await client.post("/v1/recall", json=payload)
            r.raise_for_status()

        direct_ns: list[int] = []
        http_ns: list[int] = []
        overhead_ns: list[int] = []

        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            await brain.recall(
                payload["query"],
                bank_id=payload["bank_id"],
                max_results=payload["max_results"],
                context=None,
            )
            t1 = time.perf_counter_ns()
            direct_ns.append(t1 - t0)

            t2 = time.perf_counter_ns()
            resp = await client.post("/v1/recall", json=payload)
            t3 = time.perf_counter_ns()
            resp.raise_for_status()
            http_ns.append(t3 - t2)
            overhead_ns.append((t3 - t2) - (t1 - t0))

    out: dict[str, Any] = {
        "warmup": warmup,
        "iterations": iterations,
        **_summarize("direct", direct_ns),
        **_summarize("http", http_ns),
        **_summarize("overhead", overhead_ns),
    }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmup", type=int, default=100, help="Iterations before measuring")
    parser.add_argument("--iterations", type=int, default=2000, help="Timed iterations")
    parser.add_argument("--json", action="store_true", help="Print JSON only (for CI)")
    args = parser.parse_args()

    try:
        result = asyncio.run(_run(warmup=args.warmup, iterations=args.iterations))
    except Exception as e:
        print(f"bench_gateway_overhead failed: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"Gateway overhead (HTTP ASGI − direct recall), n={result['iterations']} "
            f"(warmup={result['warmup']})\n"
            f"  overhead p50: {result['overhead_p50_ms']:.3f} ms\n"
            f"  overhead p95: {result['overhead_p95_ms']:.3f} ms\n"
            f"  overhead p99: {result['overhead_p99_ms']:.3f} ms\n"
            f"  overhead mean: {result['overhead_mean_ms']:.3f} ms\n"
            f"  (direct recall p50: {result['direct_p50_ms']:.3f} ms | "
            f"http path p50: {result['http_p50_ms']:.3f} ms)\n",
        )


if __name__ == "__main__":
    main()
