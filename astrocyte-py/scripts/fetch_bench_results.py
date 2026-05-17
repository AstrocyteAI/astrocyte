"""Fetch archived benchmark results from R2 for offline inspection.

The private bucket holds full per-question detail. This script pulls
back gzipped run JSONs and unzips them locally under
``benchmark-results/_r2/...`` so they don't collide with fresh local
runs.

Usage::

    # Latest result for one bench
    python -m scripts.fetch_bench_results --bench locomo --latest

    # Everything from a specific day
    python -m scripts.fetch_bench_results --date 2026-05-08

    # All runs for one stage across all dates
    python -m scripts.fetch_bench_results --stage pr2-d.5.5c-fix

    # Trajectory artifact (small; useful for CI/CLI trend views)
    python -m scripts.fetch_bench_results --trajectory --bench locomo
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts._r2_client import R2Config, r2_client  # type: ignore  # noqa: E402
    from scripts.archive_bench_results import _get_json, _list_keys  # type: ignore
else:
    from ._r2_client import R2Config, r2_client
    from .archive_bench_results import _get_json, _list_keys


DEFAULT_OUT = Path("benchmark-results") / "_r2"


async def _download(client, bucket: str, key: str, dest: Path) -> None:
    resp = await client.get_object(Bucket=bucket, Key=key)
    body = await resp["Body"].read()
    if key.endswith(".gz"):
        body = gzip.decompress(body)
        dest = dest.with_suffix(dest.suffix.removesuffix(".gz")) if dest.suffix == ".gz" else dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    print(f"  -> {dest} ({len(body):,} bytes)")


async def fetch_latest(bench: str, out: Path, cfg: R2Config) -> int:
    async with r2_client(cfg) as client:
        key = f"latest/{bench}.json.gz"
        local = out / "latest" / f"{bench}.json"
        await _download(client, cfg.bucket_private, key, local)
    return 0


async def fetch_by_date(date: str, out: Path, cfg: R2Config) -> int:
    async with r2_client(cfg) as client:
        keys = await _list_keys(client, cfg.bucket_private, f"runs/{date}/")
        if not keys:
            print(f"  no objects under runs/{date}/", file=sys.stderr)
            return 1
        for key in keys:
            local = out / Path(key.removeprefix("runs/"))
            if key.endswith(".gz"):
                local = local.with_suffix("")  # drop .gz from local path
            await _download(client, cfg.bucket_private, key, local)
    return 0


async def fetch_by_stage(stage: str, out: Path, cfg: R2Config) -> int:
    """Walk per-day manifests, pull entries whose stage matches."""
    n = 0
    async with r2_client(cfg) as client:
        manifest_keys = [
            k for k in await _list_keys(client, cfg.bucket_private, "runs/") if k.endswith("/manifest.json")
        ]
        for mkey in sorted(manifest_keys):
            manifest = await _get_json(client, cfg.bucket_private, mkey)
            if not manifest:
                continue
            for run in manifest.get("runs", []):
                if run.get("stage") != stage:
                    continue
                key = run["result_key"]
                local = out / Path(key.removeprefix("runs/"))
                if key.endswith(".gz"):
                    local = local.with_suffix("")
                await _download(client, cfg.bucket_private, key, local)
                n += 1
    if n == 0:
        print(f"  no runs matched stage={stage!r}", file=sys.stderr)
        return 1
    return 0


async def fetch_trajectory(bench: str, out: Path, cfg: R2Config) -> int:
    """Pull the small public trajectory artifact (no auth needed but using
    the authed client for consistency)."""
    async with r2_client(cfg) as client:
        key = f"trajectory/{bench}.json"
        local = out / "trajectory" / f"{bench}.json"
        await _download(client, cfg.bucket_public, key, local)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--bench", choices=("locomo", "longmemeval"), default=None)
    p.add_argument("--latest", action="store_true", help="Fetch latest/<bench>.json.gz (requires --bench).")
    p.add_argument("--date", default=None, help="UTC date (YYYY-MM-DD): pull every run from that day.")
    p.add_argument("--stage", default=None, help="Stage label: pull every run matching this stage.")
    p.add_argument(
        "--trajectory", action="store_true", help="Pull the small public trajectory artifact (requires --bench)."
    )
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"Output directory (default: {DEFAULT_OUT}).")
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    cfg = R2Config.from_env()

    if args.trajectory:
        if not args.bench:
            print("ERROR: --trajectory requires --bench", file=sys.stderr)
            return 2
        return await fetch_trajectory(args.bench, args.out, cfg)

    if args.latest:
        if not args.bench:
            print("ERROR: --latest requires --bench", file=sys.stderr)
            return 2
        return await fetch_latest(args.bench, args.out, cfg)

    if args.date:
        return await fetch_by_date(args.date, args.out, cfg)

    if args.stage:
        return await fetch_by_stage(args.stage, args.out, cfg)

    print("ERROR: pass one of --latest / --date / --stage / --trajectory", file=sys.stderr)
    return 2


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
