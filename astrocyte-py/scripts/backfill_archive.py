"""One-shot historical backfill of pre-archive bench results into R2.

Walks two source directories, gzips and uploads each ``results-*.json``
under a ``historical/`` prefix in the **private** bucket, then refreshes
the public trajectory artifact so the chart starts from real history
rather than from "today."

Sources::

    astrocyte-py/benchmark-results/results-*.json     -> historical/daily
    astrocyte-py/benchmark-results/results-matrix-*.json -> historical/preset-matrix
    /tmp/pr*-gate-*/results-*.json                     -> historical/pr-gates/<stage>

Stage names for ``/tmp/pr*-gate-*`` are inferred from the dir name by
splitting on the last ``-``: ``pr1-gate-locomo`` -> stage ``pr1-gate``,
bench ``locomo``.

Run once after the forward-going archive is verified, then delete the
source files (or leave them; they're already gitignored)::

    doppler run --config bench -- python -m scripts.backfill_archive --execute

Defaults to a dry run; pass ``--execute`` to actually upload.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts._r2_client import R2Config, r2_client  # type: ignore  # noqa: E402
    from scripts.archive_bench_results import (  # type: ignore
        _gzip_bytes,
        _normalize_bench_name,
        _put_object,
        _regenerate_trajectory,
        _split_per_bench,
        _summary_from_payload,
    )
else:
    from ._r2_client import R2Config, r2_client
    from .archive_bench_results import (
        _gzip_bytes,
        _normalize_bench_name,
        _put_object,
        _regenerate_trajectory,
        _split_per_bench,
        _summary_from_payload,
    )


REPO_ROOT = Path(__file__).resolve().parent.parent  # astrocyte-py/
LOCAL_RESULTS_DIR = REPO_ROOT / "benchmark-results"
PR_GATE_GLOB = "/tmp/pr*-gate-*"


_TS_RE = re.compile(r"results-(\d{8}T\d{6}Z)")


def _date_from_filename(path: Path) -> str:
    """Extract YYYY-MM-DD from `results-20260509T090738Z.json`. Falls back
    to the file's mtime if the filename doesn't match the convention."""
    m = _TS_RE.search(path.name)
    if m:
        ts = m.group(1)
        return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return mtime.strftime("%Y-%m-%d")


def _stage_and_bench_from_pr_gate_dir(dir_path: Path) -> tuple[str, str]:
    """``/tmp/pr2-d55-gate-lme`` -> (``pr2-d55-gate``, ``longmemeval``)."""
    name = dir_path.name
    # Bench is always the last segment after the last dash.
    *stage_parts, bench = name.rsplit("-", 1)
    stage = "-".join(stage_parts)
    return stage, _normalize_bench_name(bench)


def _collect_local_results() -> list[tuple[str, str, Path]]:
    """Return ``[(stage, bench_or_empty, path), ...]`` for daily + matrix files.

    bench_or_empty is empty for wrapped files (split happens at upload time).
    """
    out: list[tuple[str, str, Path]] = []
    if not LOCAL_RESULTS_DIR.exists():
        return out
    for p in sorted(LOCAL_RESULTS_DIR.glob("results-2*.json")):
        out.append(("daily", "", p))
    for p in sorted(LOCAL_RESULTS_DIR.glob("results-matrix-*.json")):
        out.append(("preset-matrix", "", p))
    return out


def _collect_pr_gate_results() -> list[tuple[str, str, Path]]:
    out: list[tuple[str, str, Path]] = []
    for d in sorted(Path("/tmp").glob("pr*-gate-*")):
        if not d.is_dir():
            continue
        stage, bench = _stage_and_bench_from_pr_gate_dir(d)
        for p in sorted(d.glob("results-*.json")):
            out.append((stage, bench, p))
    return out


async def _upload_one(
    client,
    cfg: R2Config,
    *,
    source: str,                # "daily" | "preset-matrix" | "pr-gates"
    stage: str,
    bench_hint: str,            # "" or one of KNOWN_BENCHES
    path: Path,
    dry_run: bool,
    historical_runs: list[dict],
) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  SKIP {path}: parse failed: {exc}", file=sys.stderr)
        return 0

    entries = _split_per_bench(payload)
    if not entries:
        if not bench_hint:
            print(f"  SKIP {path}: unwrapped + no bench hint", file=sys.stderr)
            return 0
        entries = [(bench_hint, payload)]

    date = _date_from_filename(path)
    n = 0
    for bench, bench_payload in entries:
        prefix = f"historical/{source}"
        if source == "pr-gates":
            key = f"{prefix}/{stage}/{bench}/{path.name}.gz"
        else:
            key = f"{prefix}/{bench}/{path.stem}.json.gz"

        gz = _gzip_bytes(json.dumps(bench_payload, default=str).encode("utf-8"))
        if dry_run:
            print(f"  DRY {bench:<12} {len(gz):>8,}B  s3://{cfg.bucket_private}/{key}")
        else:
            await _put_object(
                client, cfg.bucket_private, key, gz, "application/gzip"
            )
            print(f"  uploaded {bench:<12} -> s3://{cfg.bucket_private}/{key}")

        # Append to a synthetic "historical" manifest so the trajectory
        # artifact picks these runs up.
        historical_runs.append(
            {
                "date": date,
                "stage": f"historical-{source}" if source != "pr-gates" else stage,
                "bench": bench,
                "sha": "historical",
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "result_key": key,
                **_summary_from_payload(bench, bench_payload),
            }
        )
        n += 1
    return n


async def _write_historical_manifest(
    client, cfg: R2Config, runs: list[dict], *, dry_run: bool
) -> None:
    """Write `historical/manifest.json` so trajectory regen picks up the runs."""
    if dry_run:
        return
    body = json.dumps(
        {
            "kind": "historical",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "runs": runs,
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    await _put_object(
        client, cfg.bucket_private, "historical/manifest.json", body, "application/json"
    )


async def _amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--execute", action="store_true",
                        help="Actually upload (default: dry run).")
    parser.add_argument("--skip-pr-gates", action="store_true")
    parser.add_argument("--skip-local", action="store_true")
    args = parser.parse_args(argv)

    cfg = R2Config.from_env()
    dry_run = not args.execute
    label = "DRY RUN" if dry_run else "EXECUTING"

    sources: list[tuple[str, str, str, Path]] = []  # (source, stage, bench_hint, path)
    if not args.skip_local:
        for stage, bench, p in _collect_local_results():
            sources.append((stage, stage, bench, p))
    if not args.skip_pr_gates:
        for stage, bench, p in _collect_pr_gate_results():
            sources.append(("pr-gates", stage, bench, p))

    if not sources:
        print("  no historical results found.")
        return 0

    print(f"  {label}: {len(sources)} source files")
    historical_runs: list[dict] = []
    total = 0

    async with r2_client(cfg) as client:
        for source, stage, bench, path in sources:
            total += await _upload_one(
                client,
                cfg,
                source=source,
                stage=stage,
                bench_hint=bench,
                path=path,
                dry_run=dry_run,
                historical_runs=historical_runs,
            )
        await _write_historical_manifest(client, cfg, historical_runs, dry_run=dry_run)
        if not dry_run:
            counts = await _regenerate_trajectory(client, cfg)
            for bench, n in counts.items():
                print(f"  trajectory/{bench}.json ({n} runs)")

    print(f"  done: {total} bench-payload(s) {'staged' if dry_run else 'uploaded'}")
    if dry_run:
        print("  re-run with --execute to actually upload.")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
