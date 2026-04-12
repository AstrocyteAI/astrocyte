#!/usr/bin/env python3
"""Measure gateway-only overhead: HTTP path vs direct ``brain.recall`` (same ``Astrocyte``).

**Default (ASGI):** ``httpx.ASGITransport`` — no TCP, no uvicorn; isolates FastAPI + JSON + middleware.

**``--tcp``:** Uvicorn on ``127.0.0.1`` + ephemeral port in a **background thread**, client uses real
``http://`` loopback — adds TCP stack, HTTP parser (httptools/h11), and uvicorn worker overhead.

Core work stays in-memory + mock LLM unless you set ``ASTROCYTE_CONFIG_PATH``.

Environment: ``ASTROCYTE_AUTH_MODE=dev`` unless set; ``ASTROCYTE_CONFIG_PATH`` cleared for defaults.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import statistics
import sys
import threading
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


def _prepare_brain_and_payload() -> tuple[Any, Any, dict[str, Any]]:
    os.environ.setdefault("ASTROCYTE_AUTH_MODE", "dev")
    os.environ.pop("ASTROCYTE_CONFIG_PATH", None)

    from astrocyte_gateway.app import create_app
    from astrocyte_gateway.brain import build_astrocyte

    brain = build_astrocyte()
    app = create_app(brain)
    payload: dict[str, Any] = {"query": "bench", "bank_id": "b1", "max_results": 5}
    return brain, app, payload


async def _measure_loop(
    *,
    warmup: int,
    iterations: int,
    brain: Any,
    payload: dict[str, Any],
    http_post: Any,
) -> dict[str, Any]:
    for _ in range(warmup):
        await brain.recall(
            payload["query"],
            bank_id=payload["bank_id"],
            max_results=payload["max_results"],
            context=None,
        )
        r = await http_post()
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
        resp = await http_post()
        t3 = time.perf_counter_ns()
        resp.raise_for_status()
        http_ns.append(t3 - t2)
        overhead_ns.append((t3 - t2) - (t1 - t0))

    return {
        **_summarize("direct", direct_ns),
        **_summarize("http", http_ns),
        **_summarize("overhead", overhead_ns),
    }


async def _run_asgi(*, warmup: int, iterations: int) -> dict[str, Any]:
    import httpx

    brain, app, payload = _prepare_brain_and_payload()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def post() -> Any:
            return await client.post("/v1/recall", json=payload)

        stats = await _measure_loop(
            warmup=warmup,
            iterations=iterations,
            brain=brain,
            payload=payload,
            http_post=post,
        )
    out: dict[str, Any] = {
        "transport": "asgi_in_process",
        "warmup": warmup,
        "iterations": iterations,
        **stats,
    }
    return out


async def _run_tcp(*, warmup: int, iterations: int) -> dict[str, Any]:
    import httpx
    import uvicorn

    brain, app, payload = _prepare_brain_and_payload()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = int(sock.getsockname()[1])

    config = uvicorn.Config(
        app,
        log_level="warning",
        access_log=False,
        loop="asyncio",
        lifespan="on",
    )
    server = uvicorn.Server(config)

    def _serve() -> None:
        server.run(sockets=[sock])

    thread = threading.Thread(target=_serve, name="uvicorn-bench", daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=5.0) as sync_client:
            for _ in range(200):
                try:
                    r = sync_client.get(f"{base}/live")
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    # Server not accepting yet (connection refused, reset); retry until ready.
                    pass
                time.sleep(0.02)
            else:
                raise RuntimeError("uvicorn did not become ready on /live")

        async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:

            async def post() -> Any:
                return await client.post("/v1/recall", json=payload)

            stats = await _measure_loop(
                warmup=warmup,
                iterations=iterations,
                brain=brain,
                payload=payload,
                http_post=post,
            )
    finally:
        server.should_exit = True
        thread.join(timeout=30.0)
        if thread.is_alive():
            print("warning: uvicorn thread did not exit cleanly", file=sys.stderr)

    out: dict[str, Any] = {
        "transport": "tcp_loopback_uvicorn",
        "listen_port": port,
        "warmup": warmup,
        "iterations": iterations,
        **stats,
    }
    return out


async def _run(*, warmup: int, iterations: int, tcp: bool) -> dict[str, Any]:
    if tcp:
        return await _run_tcp(warmup=warmup, iterations=iterations)
    return await _run_asgi(warmup=warmup, iterations=iterations)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmup", type=int, default=100, help="Iterations before measuring")
    parser.add_argument("--iterations", type=int, default=2000, help="Timed iterations")
    parser.add_argument(
        "--tcp",
        action="store_true",
        help="Use uvicorn on 127.0.0.1 + real TCP (background thread); slower, closer to production",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON only (for CI)")
    args = parser.parse_args()

    try:
        result = asyncio.run(_run(warmup=args.warmup, iterations=args.iterations, tcp=args.tcp))
    except Exception as e:
        print(f"bench_gateway_overhead failed: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    transport = result.get("transport", "asgi_in_process")
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        label = "TCP+uvicorn (loopback) − direct" if args.tcp else "HTTP ASGI − direct"
        print(
            f"Gateway overhead ({label}), transport={transport}, n={result['iterations']} "
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
