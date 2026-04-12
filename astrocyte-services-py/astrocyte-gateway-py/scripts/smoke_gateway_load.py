#!/usr/bin/env python3
"""Concurrent HTTP smoke against a running gateway. Exits non-zero if any request is not HTTP 200.

Used by CI (optional workflow_dispatch) and locally against ``uvicorn`` on loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter


async def _run(
    *,
    base_url: str,
    path: str,
    total: int,
    concurrency: int,
    warmup: int,
) -> int:
    import httpx

    url = base_url.rstrip("/") + path
    body = {"query": "smoke", "bank_id": "b1", "max_results": 5}

    sem = asyncio.Semaphore(concurrency)

    async def one(client: httpx.AsyncClient, _: int) -> tuple[int, float]:
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await client.post(url, json=body)
                return r.status_code, time.perf_counter() - t0
            except httpx.HTTPError:
                return 0, time.perf_counter() - t0

    async with httpx.AsyncClient(timeout=60.0) as client:
        if warmup > 0:
            await asyncio.gather(*[one(client, i) for i in range(warmup)])

        t0_wall = time.perf_counter()
        rows = await asyncio.gather(*[one(client, i) for i in range(total)])
        wall_s = time.perf_counter() - t0_wall

    codes = [c for c, _ in rows]
    times_s = [dt for _, dt in rows]
    bad = [c for c in codes if c != 200]

    def pct(q: float) -> float:
        s = sorted(times_s)
        if not s:
            return 0.0
        idx = int(q * (len(s) - 1))
        return s[idx] * 1000.0

    summary = {
        "url": url,
        "total": total,
        "warmup": warmup,
        "concurrency": concurrency,
        "wall_seconds": round(wall_s, 4),
        "rps": round(total / wall_s, 2) if wall_s > 0 else 0.0,
        "all_200": len(bad) == 0,
        "status_codes": dict(Counter(codes)),
        "latency_ms_p50": round(pct(0.50), 3),
        "latency_ms_p95": round(pct(0.95), 3),
        "latency_ms_p99": round(pct(0.99), 3),
        "latency_ms_mean": round(statistics.mean(times_s) * 1000.0, 3) if times_s else 0.0,
    }

    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    sys.stdout.flush()
    if bad:
        print(f"error: {len(bad)}/{total} requests were not HTTP 200", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://127.0.0.1:18080", help="Gateway base URL")
    p.add_argument("--path", default="/v1/recall", help="POST path")
    p.add_argument("--requests", type=int, default=200, help="Total POSTs after warmup")
    p.add_argument("--concurrency", type=int, default=10, help="Max in-flight requests")
    p.add_argument("--warmup", type=int, default=20, help="Warmup POSTs (not counted in stats)")
    args = p.parse_args()

    code = asyncio.run(
        _run(
            base_url=args.base_url,
            path=args.path,
            total=args.requests,
            concurrency=args.concurrency,
            warmup=args.warmup,
        )
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
