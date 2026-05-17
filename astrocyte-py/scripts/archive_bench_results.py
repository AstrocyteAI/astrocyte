"""Archive benchmark results to Cloudflare R2.

Uploads ``results-*.json`` files to the private bucket
(``runs/<date>/<stage>/<bench>/...``), updates the per-day manifest,
copies to ``latest/``, and regenerates the public ``trajectory/<bench>.json``
artifact the docs site reads.

See ``docs/_design/bench-archive.md`` for the full layout.

Usage::

    # Archive one or more result files under a labelled stage
    python -m scripts.archive_bench_results \\
        --stage local-ad-hoc --files benchmark-results/results-*.json

    # Rebuild only the public trajectory artifact (after a wipe)
    python -m scripts.archive_bench_results --rebuild-trajectory

    # Verify both buckets are reachable (CI smoke / manual sanity)
    python -m scripts.archive_bench_results --selftest
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import io
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Allow `python -m scripts.archive_bench_results` and direct invocation.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts._r2_client import R2Config, r2_client  # type: ignore  # noqa: E402
else:
    from ._r2_client import R2Config, r2_client


KNOWN_BENCHES = ("locomo", "longmemeval")


# ---------------------------------------------------------------------------
# Result-file shape detection
# ---------------------------------------------------------------------------


def _split_per_bench(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return ``[(bench_name, bench_payload), ...]`` from one results JSON.

    Handles two on-disk shapes:

    1. **Wrapped** (current ``run_benchmarks.py`` output)::

           {"locomo": {...}, "longmemeval": {...}, "_meta": {...}}

       Each top-level key (other than ``_meta``) is one bench's payload.

    2. **Unwrapped** (older ``/tmp/pr*-gate-*/results-*.json`` from
       hand-rolled gate runs)::

           {"overall_accuracy": ..., "category_accuracy": {...},
            "results": [...]}

       Single-bench file. The bench name has to be supplied externally
       (CLI ``--bench`` flag, or inferred from the source dir/filename).
    """
    benches: list[tuple[str, dict[str, Any]]] = []
    if "overall_accuracy" in payload and "category_accuracy" in payload:
        # Unwrapped: caller must supply bench name
        return []
    for key, value in payload.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and ("overall_accuracy" in value or "benchmark" in value or "metrics" in value):
            benches.append((_normalize_bench_name(key), value))
    return benches


def _normalize_bench_name(name: str) -> str:
    """Map various aliases to the canonical bench name."""
    n = name.lower().strip()
    if n in ("lme", "longmemeval", "long_memeval", "long-mem-eval"):
        return "longmemeval"
    if n == "locomo":
        return "locomo"
    return n


def _short_sha() -> str:
    """Return short git SHA, or 'dirty' if working tree has changes,
    or 'unknown' if not in a repo."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if sha.returncode != 0:
            return "unknown"
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if dirty.stdout.strip():
            return "dirty"
        return sha.stdout.strip()
    except Exception:
        return "unknown"


def _gzip_bytes(data: bytes) -> bytes:
    buf = io.BytesIO()
    # mtime=0 so identical content yields identical bytes (helps dedup at the
    # storage layer if R2 ever exposes object-level dedup, and keeps tests
    # deterministic).
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(data)
    return buf.getvalue()


def _summary_from_payload(bench: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the small set of fields the trajectory artifact needs."""
    cats = payload.get("category_accuracy") or {}
    return {
        "bench": bench,
        "overall": payload.get("overall_accuracy"),
        "categories": {k: round(float(v), 4) for k, v in cats.items()},
        "n_questions": payload.get("evaluated_questions") or payload.get("total_questions"),
        "judge": "llm" if payload.get("canonical_judge") or payload.get("judge_model") else "stemmed-f1",
        "model": payload.get("model"),
    }


# ---------------------------------------------------------------------------
# R2 operations
# ---------------------------------------------------------------------------


async def _put_object(client, bucket: str, key: str, body: bytes, content_type: str) -> None:
    await client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
    )


async def _get_json(client, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        resp = await client.get_object(Bucket=bucket, Key=key)
    except client.exceptions.NoSuchKey:
        return None
    except Exception as exc:
        # botocore wraps 404 as ClientError with code 'NoSuchKey' on R2.
        if "NoSuchKey" in str(exc) or "404" in str(exc):
            return None
        raise
    body = await resp["Body"].read()
    return json.loads(body.decode("utf-8"))


async def _list_keys(client, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    continuation: str | None = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = await client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []) or []:
            keys.append(obj["Key"])
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
    return keys


# ---------------------------------------------------------------------------
# Manifest + trajectory regeneration
# ---------------------------------------------------------------------------


async def _update_day_manifest(client, cfg: R2Config, *, date: str, entry: dict[str, Any]) -> None:
    """Read-modify-write the per-day manifest. Single-writer assumption.

    NOTE: For the post-run hook (single-process) and the historical
    backfill (sequential), single-writer holds. If multiple processes
    ever archive concurrently, replace with a small lock object or an
    ``If-Match: <etag>`` retry loop.
    """
    key = f"runs/{date}/manifest.json"
    manifest = await _get_json(client, cfg.bucket_private, key) or {
        "date": date,
        "runs": [],
    }
    manifest["runs"].append(entry)
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    body = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    await _put_object(client, cfg.bucket_private, key, body, "application/json")


async def _regenerate_trajectory(client, cfg: R2Config) -> dict[str, int]:
    """Walk every per-day manifest (plus the historical backfill manifest if
    present) and write one trajectory JSON per bench to the public bucket.
    Returns ``{bench: run_count}``."""
    manifest_keys = [k for k in await _list_keys(client, cfg.bucket_private, "runs/") if k.endswith("/manifest.json")]
    # Pick up the historical backfill manifest (single file, optional).
    if await _get_json(client, cfg.bucket_private, "historical/manifest.json"):
        manifest_keys.append("historical/manifest.json")
    by_bench: dict[str, list[dict[str, Any]]] = {b: [] for b in KNOWN_BENCHES}
    for key in sorted(manifest_keys):
        manifest = await _get_json(client, cfg.bucket_private, key)
        if not manifest:
            continue
        for run in manifest.get("runs", []):
            bench = run.get("bench")
            if bench not in by_bench:
                by_bench[bench] = []
            by_bench[bench].append(run)

    counts: dict[str, int] = {}
    now = datetime.now(timezone.utc).isoformat()
    for bench, runs in by_bench.items():
        runs_sorted = sorted(runs, key=lambda r: (r.get("date", ""), r.get("uploaded_at", "")))
        artifact = {
            "bench": bench,
            "updated_at": now,
            "runs": runs_sorted,
        }
        body = json.dumps(artifact, indent=2).encode("utf-8")
        await _put_object(client, cfg.bucket_public, f"trajectory/{bench}.json", body, "application/json")
        counts[bench] = len(runs_sorted)
    return counts


# ---------------------------------------------------------------------------
# High-level archive
# ---------------------------------------------------------------------------


async def archive_files(
    files: Iterable[Path],
    *,
    stage: str,
    bench_override: str | None = None,
    cfg: R2Config | None = None,
    historical_prefix: str | None = None,
) -> int:
    """Archive each file, returning the number of bench-payloads uploaded.

    ``historical_prefix`` (e.g. ``"historical/pr-gates"``) overrides the
    forward-going ``runs/<date>/<stage>`` layout for backfill runs.
    """
    cfg = cfg or R2Config.from_env()
    sha = _short_sha()
    uploaded = 0

    async with r2_client(cfg) as client:
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"  SKIP {path}: parse failed: {exc}", file=sys.stderr)
                continue

            entries = _split_per_bench(payload)
            if not entries:
                if not bench_override:
                    print(
                        f"  SKIP {path}: unwrapped shape and no --bench override",
                        file=sys.stderr,
                    )
                    continue
                entries = [(_normalize_bench_name(bench_override), payload)]

            for bench, bench_payload in entries:
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                date = ts[:4] + "-" + ts[4:6] + "-" + ts[6:8]
                if historical_prefix:
                    key = f"{historical_prefix}/{stage}/{bench}/{path.name}.gz"
                else:
                    key = f"runs/{date}/{stage}/{bench}/results-{ts}-{sha}.json.gz"

                gz = _gzip_bytes(json.dumps(bench_payload, default=str).encode("utf-8"))
                await _put_object(client, cfg.bucket_private, key, gz, "application/gzip")

                # Manifest only updated for forward-going `runs/...` layout.
                # Historical backfill aggregates separately via trajectory.
                if not historical_prefix:
                    await _update_day_manifest(
                        client,
                        cfg,
                        date=date,
                        entry={
                            "date": date,
                            "stage": stage,
                            "bench": bench,
                            "sha": sha,
                            "uploaded_at": datetime.now(timezone.utc).isoformat(),
                            "result_key": key,
                            **_summary_from_payload(bench, bench_payload),
                        },
                    )
                    # Refresh latest/<bench>.json.gz
                    await _put_object(
                        client,
                        cfg.bucket_private,
                        f"latest/{bench}.json.gz",
                        gz,
                        "application/gzip",
                    )

                uploaded += 1
                print(f"  uploaded {bench:<12} -> s3://{cfg.bucket_private}/{key}")

        # Always refresh the public trajectory artifact at end-of-batch.
        if uploaded > 0 or historical_prefix:
            counts = await _regenerate_trajectory(client, cfg)
            for bench, n in counts.items():
                print(f"  trajectory/{bench}.json -> {cfg.public_url}/trajectory/{bench}.json ({n} runs)")

    return uploaded


async def selftest(cfg: R2Config | None = None) -> int:
    """Verify both buckets are reachable for read+write."""
    cfg = cfg or R2Config.from_env()
    probe_key = f"_selftest/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.txt"
    body = b"ok"
    async with r2_client(cfg) as client:
        # Private bucket round-trip
        await _put_object(client, cfg.bucket_private, probe_key, body, "text/plain")
        resp = await client.get_object(Bucket=cfg.bucket_private, Key=probe_key)
        assert (await resp["Body"].read()) == body
        await client.delete_object(Bucket=cfg.bucket_private, Key=probe_key)
        # Public bucket round-trip
        await _put_object(client, cfg.bucket_public, probe_key, body, "text/plain")
        resp = await client.get_object(Bucket=cfg.bucket_public, Key=probe_key)
        assert (await resp["Body"].read()) == body
        await client.delete_object(Bucket=cfg.bucket_public, Key=probe_key)
    print(f"  OK private={cfg.bucket_private} public={cfg.bucket_public}")
    print(f"  OK public_url={cfg.public_url}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--files", nargs="*", type=Path, default=[], help="Result JSON files to upload.")
    p.add_argument("--stage", default="local-ad-hoc", help="Stage label (e.g. weekly-ci, pr1-gate, local-ad-hoc).")
    p.add_argument(
        "--bench",
        default=None,
        help="Override bench name when the result file is unwrapped (e.g. /tmp/pr*-gate-*/results-*.json).",
    )
    p.add_argument(
        "--rebuild-trajectory",
        action="store_true",
        help="Skip uploads; regenerate trajectory/<bench>.json from existing manifests in the private bucket.",
    )
    p.add_argument("--selftest", action="store_true", help="Verify both buckets are reachable; exit 0 on success.")
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    cfg = R2Config.from_env()

    if args.selftest:
        return await selftest(cfg)

    if args.rebuild_trajectory:
        async with r2_client(cfg) as client:
            counts = await _regenerate_trajectory(client, cfg)
        for bench, n in counts.items():
            print(f"  trajectory/{bench}.json ({n} runs)")
        return 0

    if not args.files:
        print("ERROR: --files is required (or pass --selftest / --rebuild-trajectory)", file=sys.stderr)
        return 2

    expanded: list[Path] = []
    for f in args.files:
        if f.is_dir():
            expanded.extend(sorted(f.glob("results-*.json")))
        else:
            expanded.append(f)
    if not expanded:
        print("ERROR: no result files found.", file=sys.stderr)
        return 2

    n = await archive_files(expanded, stage=args.stage, bench_override=args.bench, cfg=cfg)
    print(f"  archived {n} bench-payload(s) across {len(expanded)} file(s)")
    return 0 if n > 0 else 1


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
