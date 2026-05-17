"""One-shot cleanup for the misformed M18b archive.

The 2026-05-17 ``runs/2026-05-17/m18b-close/`` archive captured the
wrong runs:

- A single ``locomo/`` and ``longmemeval/`` result that scored 62.5% /
  56.5% — these were NOT the SHIPPED B1-dp+RRF runs (which scored
  85.5% / 82% under the Mem0-harness path).
- 40+ ``metadata/`` sidecar JSONs with ``bench="metadata"`` and
  ``overall=null``, which polluted the public bucket as
  ``trajectory/metadata.json``.

This script removes:

1. Every key under ``runs/2026-05-17/m18b-close/`` in the private bucket.
2. The matching entries in ``runs/2026-05-17/manifest.json`` (so the
   trajectory regenerator doesn't pick them up again).
3. The orphaned ``trajectory/metadata.json`` from the public bucket.

After running, do ``make bench-archive-rescan`` to re-archive the
correct M18b Mem0-harness runs under per-condition stages, then
``make bench-archive-rebuild-trajectory`` to refresh the public artifact.

Usage::

    doppler run --config bench -- uv run python -m scripts.wipe_bad_m18b_archive --dry-run
    doppler run --config bench -- uv run python -m scripts.wipe_bad_m18b_archive --execute
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts._r2_client import R2Config, r2_client  # type: ignore  # noqa: E402
    from scripts.archive_bench_results import _get_json, _list_keys, _put_object  # type: ignore  # noqa: E402
else:
    from ._r2_client import R2Config, r2_client
    from .archive_bench_results import _get_json, _list_keys, _put_object


BAD_PREFIX = "runs/2026-05-17/m18b-close/"
BAD_MANIFEST_KEY = "runs/2026-05-17/manifest.json"
BAD_PUBLIC_KEY = "trajectory/metadata.json"


async def _amain(execute: bool) -> int:
    cfg = R2Config.from_env()
    async with r2_client(cfg) as client:
        # 1. List + delete private-bucket keys under the bad prefix.
        keys = await _list_keys(client, cfg.bucket_private, BAD_PREFIX)
        print(f"  private: {len(keys)} keys under {BAD_PREFIX}")
        if keys and execute:
            for k in keys:
                await client.delete_object(Bucket=cfg.bucket_private, Key=k)
                print(f"    deleted s3://{cfg.bucket_private}/{k}")
        elif keys:
            for k in keys[:5]:
                print(f"    DRY would delete s3://{cfg.bucket_private}/{k}")
            if len(keys) > 5:
                print(f"    ... and {len(keys) - 5} more")

        # 2. Prune bogus entries from the per-day manifest (keep proper entries if any).
        manifest = await _get_json(client, cfg.bucket_private, BAD_MANIFEST_KEY)
        if manifest:
            runs = manifest.get("runs", [])
            keep = [
                r for r in runs
                if r.get("bench") in ("locomo", "longmemeval")
                and r.get("stage") != "m18b-close"
                and r.get("overall") is not None
            ]
            removed = len(runs) - len(keep)
            print(f"  manifest {BAD_MANIFEST_KEY}: {removed} entries to prune, {len(keep)} kept")
            if removed and execute:
                manifest["runs"] = keep
                body = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
                await _put_object(client, cfg.bucket_private, BAD_MANIFEST_KEY, body, "application/json")
                print(f"    rewrote {BAD_MANIFEST_KEY}")
            elif removed:
                print(f"    DRY would prune {removed} entries")

        # 3. Public bucket: drop the trajectory/metadata.json that the bogus
        #    runs caused us to emit.
        try:
            await client.head_object(Bucket=cfg.bucket_public, Key=BAD_PUBLIC_KEY)
            present = True
        except Exception:
            present = False
        if present:
            print(f"  public: orphaned {BAD_PUBLIC_KEY} present")
            if execute:
                await client.delete_object(Bucket=cfg.bucket_public, Key=BAD_PUBLIC_KEY)
                print(f"    deleted public/{BAD_PUBLIC_KEY}")
            else:
                print(f"    DRY would delete public/{BAD_PUBLIC_KEY}")
        else:
            print(f"  public: {BAD_PUBLIC_KEY} not present (already clean)")

    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Preview without deleting anything.")
    g.add_argument("--execute", action="store_true", help="Actually delete the bad archive entries.")
    args = p.parse_args()
    sys.exit(asyncio.run(_amain(execute=args.execute)))


if __name__ == "__main__":
    main()
