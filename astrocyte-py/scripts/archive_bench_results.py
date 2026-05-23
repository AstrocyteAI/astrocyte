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

# Canonical root for all on-disk results. The rescan walker traverses
# every subdirectory; each ``<harness>/<bench>/<project>/`` leaf is
# treated as one logical "run".
CANONICAL_RESULTS_ROOT = Path("benchmark-results")

# When a run is successfully uploaded to R2, we drop this filename next
# to the result JSON so a future rescan skips it. Delete the marker to
# force re-archive.
ARCHIVED_MARKER = "_ARCHIVED"

# A successful cycle close can mark its shipping run pair with
# ``_SHIP_LABEL.json``. The label propagates into the manifest entry and
# the trajectory regenerator means scores within the most-recent label
# group to drive the README badges. See ``scripts/mark_shipped.py``.
SHIP_LABEL_FILE = "_SHIP_LABEL.json"

# Mem0-harness result schemas expose a ``metrics_by_cutoff`` block.
#
# Cutoff history:
# - Pre-M35 cycles (m18b/m19/m30c/m31/m32/m33/m34) used item-count
#   cutoffs (``top_N``) — ``top_20`` was the canonical headline number
#   for that era's bench-close ablations.
# - M35 (v0.15.0) migrated the harness to token-budget cutoffs
#   (``max_tokens_N``) — the legacy ``top_*`` keys disappear from new
#   result JSONs.
# - M44 (v0.15.0 close) anchored the ship-floor convention on
#   ``max_tokens_8192`` — see ``docs/_design/v0.15.0-ship-decision.md``
#   Appendix A.
#
# ``DEFAULT_MEM0_CUTOFF`` is the *first-choice* cutoff. If a result JSON
# doesn't carry it, the lookup falls back through ``MEM0_CUTOFF_FALLBACKS``
# so pre-M35 archives still parse cleanly (their summaries are needed for
# trajectory continuity across the M35 boundary). Operators can override
# the first-choice via ``--mem0-cutoff`` on rescan / archive commands.
DEFAULT_MEM0_CUTOFF = "max_tokens_8192"
MEM0_CUTOFF_FALLBACKS: tuple[str, ...] = (
    "max_tokens_4096",  # next-densest token budget if 8192 missing
    "top_20",           # pre-M35 legacy headline cutoff
)

# Smoke / micro runs (n < SMOKE_MIN_QUESTIONS, or stage containing ``smoke``)
# distort the trajectory and badge color when they happen to be the most
# recent archived run. They're filtered out by the rescan by default;
# pass ``--include-smoke`` to keep them.
SMOKE_MIN_QUESTIONS = 30


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


def _summary_from_mem0(payload: dict[str, Any], cutoff: str = DEFAULT_MEM0_CUTOFF) -> dict[str, Any] | None:
    """Translate the Mem0-harness ``metrics_by_cutoff`` schema into the
    trajectory shape.

    Mem0-harness files look like::

        {"metadata": {"benchmark": "locomo", "answerer_model": "gpt-4o-mini", ...},
         "metrics_by_cutoff": {
             "top_20": {"overall": {"total": 200, "correct": 171, "accuracy": 85.5}, ...},
             ...
         },
         "evaluations": [...]}

    Returns ``None`` when the payload doesn't carry the expected shape.
    """
    meta = payload.get("metadata") or {}
    cutoffs = payload.get("metrics_by_cutoff") or {}
    # Walk the cutoff priority list: caller-supplied first-choice, then
    # the canonical fallback chain (next-densest token budget, legacy
    # ``top_20``). Lets a single archive sweep handle both M35+ token-
    # budget results and pre-M35 ``top_N`` legacy archives without the
    # operator having to choose per-project — the script just picks
    # whichever cutoff the JSON actually contains.
    chosen_cutoff = cutoff
    section = (cutoffs.get(chosen_cutoff) or {}).get("overall") or {}
    if not section:
        for fallback in MEM0_CUTOFF_FALLBACKS:
            if fallback == chosen_cutoff:
                continue
            candidate = (cutoffs.get(fallback) or {}).get("overall") or {}
            if candidate:
                section = candidate
                chosen_cutoff = fallback
                break
    if not section:
        return None
    accuracy_pct = section.get("accuracy")
    if accuracy_pct is None:
        return None
    cats_block = (cutoffs.get(chosen_cutoff) or {}).get("categories") or {}
    cats: dict[str, float] = {}
    for name, body in cats_block.items():
        if isinstance(body, dict) and "accuracy" in body:
            try:
                cats[str(name)] = round(float(body["accuracy"]) / 100, 4)
            except (TypeError, ValueError):
                continue
    return {
        "bench": _normalize_bench_name(meta.get("benchmark") or ""),
        "overall": round(float(accuracy_pct) / 100, 4),
        "categories": cats,
        "n_questions": section.get("total"),
        "judge": "llm" if meta.get("judge_model") else "stemmed-f1",
        "model": meta.get("answerer_model") or meta.get("model"),
        "cutoff": chosen_cutoff,
        "project_name": meta.get("project_name"),
        "run_id": meta.get("run_id"),
    }


# ---------------------------------------------------------------------------
# Canonical-tree rescan
# ---------------------------------------------------------------------------


def _stage_from_project_dir(dir_name: str) -> str:
    """Project dir → trajectory stage name.

    ``astrocyte-m18b-b1-dp-rrf-run-1`` -> ``m18b-b1-dp-rrf-run-1``.
    ``local-ad-hoc``                   -> ``local-ad-hoc``.
    """
    n = dir_name.strip()
    if n.startswith("astrocyte-"):
        n = n[len("astrocyte-"):]
    return n or "local-ad-hoc"


def _iter_canonical_runs(root: Path) -> Iterable[Path]:
    """Yield each ``<harness>/<bench>/<project>/`` leaf containing a
    result file. Order is deterministic (sorted depth-first)."""
    if not root.exists():
        return
    # Pattern: benchmark-results/<harness>/<bench>/<project>/
    # We accept either ``*_results_*.json`` (Mem0-harness) or
    # ``results-*.json`` (PageIndex harness, legacy flat layout).
    seen: set[Path] = set()
    candidates = sorted(root.rglob("*_results_*.json")) + sorted(root.rglob("results-*.json"))
    for f in candidates:
        # Skip backfilled R2 fetches — they live under benchmark-results/_r2/.
        if any(p == "_r2" for p in f.parts):
            continue
        leaf = f.parent
        if leaf in seen:
            continue
        seen.add(leaf)
        yield leaf


def _project_already_archived(leaf: Path) -> bool:
    return (leaf / ARCHIVED_MARKER).exists()


def _read_ship_label(leaf: Path) -> dict[str, Any] | None:
    """Return the contents of ``_SHIP_LABEL.json`` if present, else None."""
    path = leaf / SHIP_LABEL_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_archived_marker(leaf: Path) -> dict[str, Any] | None:
    """Return the contents of ``_ARCHIVED`` if present, else None.

    The marker records the R2 keys this project was uploaded to — used
    to look up the matching manifest entry for in-place ship_label
    refreshes (no re-upload).
    """
    path = leaf / ARCHIVED_MARKER
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _mark_project_archived(leaf: Path, *, result_keys: list[str]) -> None:
    """Write the ``_ARCHIVED`` marker with the R2 keys we uploaded."""
    marker = leaf / ARCHIVED_MARKER
    marker.write_text(
        json.dumps(
            {
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "result_keys": sorted(result_keys),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _load_payload_for_leaf(leaf: Path) -> tuple[dict[str, Any], Path] | None:
    """Pick the most recent result JSON in the project directory."""
    candidates = sorted(leaf.glob("*_results_*.json")) + sorted(leaf.glob("results-*.json"))
    candidates = [c for c in candidates if c.name != ARCHIVED_MARKER]
    if not candidates:
        return None
    # Most recent by mtime — handles re-runs that produce multiple
    # result files within one project directory.
    chosen = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        return json.loads(chosen.read_text(encoding="utf-8")), chosen
    except Exception as exc:
        print(f"  SKIP {chosen}: parse failed: {exc}", file=sys.stderr)
        return None


async def refresh_ship_labels(
    *,
    root: Path = CANONICAL_RESULTS_ROOT,
    cfg: R2Config | None = None,
    dry_run: bool = False,
) -> int:
    """Walk the canonical tree and surgically patch ship_label fields
    into already-archived manifest entries. No re-upload — only updates
    the per-day manifest JSONs in-place.

    Returns the number of manifest entries updated.
    """
    cfg = cfg or R2Config.from_env()
    updated = 0
    # Group projects-with-labels by date (parsed from result_key) so we
    # touch each per-day manifest at most once.
    by_date: dict[str, list[tuple[Path, dict[str, Any], list[str]]]] = {}
    for leaf in _iter_canonical_runs(root):
        label = _read_ship_label(leaf)
        archived = _read_archived_marker(leaf)
        if not label or not archived:
            continue
        keys = archived.get("result_keys") or []
        for key in keys:
            # key shape: runs/<date>/<stage>/<bench>/results-<ts>-<sha>.json.gz
            if not key.startswith("runs/"):
                continue
            date = key.split("/", 2)[1]
            by_date.setdefault(date, []).append((leaf, label, [key]))

    if not by_date:
        print("  no projects carry _SHIP_LABEL.json (nothing to refresh)")
        return 0

    async with r2_client(cfg) as client:
        for date in sorted(by_date.keys()):
            manifest_key = f"runs/{date}/manifest.json"
            manifest = await _get_json(client, cfg.bucket_private, manifest_key)
            if not manifest:
                print(f"  SKIP {manifest_key}: not found in private bucket")
                continue
            runs = manifest.get("runs", [])
            mutated = False
            for leaf, label, keys in by_date[date]:
                target_keys = set(keys)
                for run_entry in runs:
                    if run_entry.get("result_key") not in target_keys:
                        continue
                    new_label = label.get("label")
                    new_marked_at = label.get("marked_at")
                    if (
                        run_entry.get("ship_label") == new_label
                        and run_entry.get("ship_marked_at") == new_marked_at
                    ):
                        continue
                    run_entry["ship_label"] = new_label
                    run_entry["ship_marked_at"] = new_marked_at
                    if label.get("rationale"):
                        run_entry["ship_rationale"] = label["rationale"]
                    mutated = True
                    updated += 1
                    print(f"  patched {leaf} -> manifest {date} ship_label={new_label!r}")
            if mutated and not dry_run:
                manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
                body = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
                await _put_object(client, cfg.bucket_private, manifest_key, body, "application/json")

        if updated and not dry_run:
            # Regenerate trajectory + badges so the new labels take effect immediately.
            counts = await _regenerate_trajectory(client, cfg)
            for bench, n in counts.items():
                print(f"  trajectory/{bench}.json -> {cfg.public_url}/trajectory/{bench}.json ({n} runs)")

    print(f"  refresh-labels done: {updated} manifest entries patched across {len(by_date)} day(s)")
    return updated


async def archive_rescan(
    *,
    root: Path = CANONICAL_RESULTS_ROOT,
    cfg: R2Config | None = None,
    mem0_cutoff: str = DEFAULT_MEM0_CUTOFF,
    dry_run: bool = False,
    force: bool = False,
    include_smoke: bool = False,
) -> int:
    """Walk the canonical results tree and archive every project that
    doesn't already carry an ``_ARCHIVED`` marker.

    Stage names are derived from the project directory. The bench name
    is read from the payload (Mem0 schema) or inferred from the parent
    directory (PageIndex schema).

    Returns the number of payloads uploaded.
    """
    cfg = cfg or R2Config.from_env()
    sha = _short_sha()
    uploaded = 0
    skipped = 0

    leaves = list(_iter_canonical_runs(root))
    if not leaves:
        print(f"  no result files found under {root}/")
        return 0

    print(f"  scanning {len(leaves)} project director{'y' if len(leaves) == 1 else 'ies'} under {root}/")

    async with r2_client(cfg) as client:
        for leaf in leaves:
            relative = leaf.relative_to(root) if leaf.is_relative_to(root) else leaf
            # Canonical layout requires <harness>/<bench>/<project>/ (3 segments).
            # Older flat result files live at the root or under
            # benchmark-results/<bench>/ without a project dir — skip them, they
            # need to be re-organised into the canonical layout before archive.
            if len(relative.parts) < 3:
                print(f"  SKIP {leaf}: non-canonical layout (expected <harness>/<bench>/<project>/)")
                continue
            harness = relative.parts[0]
            bench_from_path = _normalize_bench_name(relative.parts[1])
            project_dir = relative.parts[-1]
            stage = _stage_from_project_dir(project_dir)

            if not force and _project_already_archived(leaf):
                skipped += 1
                continue

            loaded = _load_payload_for_leaf(leaf)
            if loaded is None:
                continue
            payload, source_file = loaded

            # Schema dispatch — Mem0 first (richer), fall back to PageIndex.
            summary = _summary_from_mem0(payload, cutoff=mem0_cutoff)
            if summary:
                bench = summary["bench"] or bench_from_path or "unknown"
                bench_payload = payload  # archive the full file
            elif "overall_accuracy" in payload:
                bench = bench_from_path or _normalize_bench_name(payload.get("benchmark") or "")
                summary = _summary_from_payload(bench, payload)
                bench_payload = payload
            else:
                print(f"  SKIP {source_file}: unrecognised schema (no metrics_by_cutoff or overall_accuracy)")
                continue

            if bench not in KNOWN_BENCHES:
                print(f"  SKIP {source_file}: bench={bench!r} not in KNOWN_BENCHES")
                continue

            # Smoke filter — drop tiny / smoke-named runs unless the
            # caller explicitly opts them in. These otherwise show up
            # as 0% / 100% / 67% extreme points on the trajectory and
            # can swing the badge color when "latest" is a smoke.
            if not include_smoke:
                n_q = summary.get("n_questions")
                if "smoke" in stage.lower():
                    print(f"  SKIP {leaf}: smoke stage (stage={stage}); pass --include-smoke to archive")
                    continue
                if isinstance(n_q, int) and n_q < SMOKE_MIN_QUESTIONS:
                    print(f"  SKIP {leaf}: micro run (n={n_q} < {SMOKE_MIN_QUESTIONS}); pass --include-smoke to archive")
                    continue

            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            date = ts[:4] + "-" + ts[4:6] + "-" + ts[6:8]
            key = f"runs/{date}/{stage}/{bench}/results-{ts}-{sha}.json.gz"

            if dry_run:
                print(f"  DRY  would upload {harness}/{bench}/{project_dir}/  ->  s3://{cfg.bucket_private}/{key}  ({summary.get('overall')})")
                uploaded += 1
                continue

            gz = _gzip_bytes(json.dumps(bench_payload, default=str).encode("utf-8"))
            await _put_object(client, cfg.bucket_private, key, gz, "application/gzip")
            ship_label = _read_ship_label(leaf)
            manifest_entry: dict[str, Any] = {
                "date": date,
                "stage": stage,
                "bench": bench,
                "sha": sha,
                "harness": harness,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "result_key": key,
                **summary,
            }
            if ship_label:
                manifest_entry["ship_label"] = ship_label.get("label")
                manifest_entry["ship_marked_at"] = ship_label.get("marked_at")
                if ship_label.get("rationale"):
                    manifest_entry["ship_rationale"] = ship_label["rationale"]
            await _update_day_manifest(
                client,
                cfg,
                date=date,
                entry=manifest_entry,
            )
            await _put_object(
                client, cfg.bucket_private,
                f"latest/{bench}.json.gz",
                gz, "application/gzip",
            )
            _mark_project_archived(leaf, result_keys=[key])
            uploaded += 1
            print(f"  uploaded {bench:<12} stage={stage:<32} overall={summary.get('overall')} -> {key}")

        if uploaded > 0 and not dry_run:
            counts = await _regenerate_trajectory(client, cfg)
            for bench, n in counts.items():
                print(f"  trajectory/{bench}.json -> {cfg.public_url}/trajectory/{bench}.json ({n} runs)")

    print(f"  rescan done: uploaded={uploaded} skipped={skipped} (already-archived) total_scanned={len(leaves)}")
    return uploaded


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

        # Badge — shields.io endpoint format. Prefers the mean of the
        # most-recent SHIPPED run pair (curated via mark_shipped); falls
        # back to the latest non-smoke run when no shipped label exists.
        badge = _build_badge_payload(bench, runs_sorted)
        if badge:
            badge_body = json.dumps(badge).encode("utf-8")
            await _put_object(
                client, cfg.bucket_public,
                f"badges/{bench}.json",
                badge_body, "application/json",
            )
    return counts


# ---------------------------------------------------------------------------
# Badge writer (shields.io endpoint format)
# ---------------------------------------------------------------------------


_BENCH_LABEL = {"locomo": "LoCoMo", "longmemeval": "LongMemEval"}

# Headline package whose latest release drives the badge label when
# BENCH_PARITY.yaml has entries. Other released packages (postgres,
# gateway-py, ...) typically ship the same cycle simultaneously so this
# choice is mostly cosmetic — the score is the same regardless.
_BADGE_HEADLINE_PACKAGE = "astrocyte"


def _find_repo_root_for_parity() -> Path | None:
    """Walk up from this script until finding BENCH_PARITY.yaml. None if missing."""
    cur = Path(__file__).resolve()
    for parent in (cur, *cur.parents):
        candidate = parent / "BENCH_PARITY.yaml"
        if candidate.exists():
            return parent
    return None


def _latest_release_for_badge(bench: str) -> dict[str, Any] | None:
    """Return the latest astrocyte release row that has scores for ``bench``,
    or None if BENCH_PARITY.yaml is missing / empty / lacks the bench.
    """
    root = _find_repo_root_for_parity()
    if root is None:
        return None
    try:
        import yaml  # local import — only needed when YAML is present
    except ImportError:
        return None
    try:
        body = yaml.safe_load((root / "BENCH_PARITY.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    releases = body.get("releases") or []
    # Prefer the headline package; fall back to any package if absent.
    headline = [r for r in releases if r.get("package") == _BADGE_HEADLINE_PACKAGE]
    candidates = headline or releases
    # Filter to releases that recorded the requested bench
    candidates = [r for r in candidates if bench in ((r.get("scores") or {}))]
    if not candidates:
        return None
    # Already sorted desc on disk, but be defensive.
    candidates.sort(key=lambda r: (r.get("released_at", ""), r.get("package", "")), reverse=True)
    return candidates[0]


def _badge_color(acc: float) -> str:
    return (
        "brightgreen" if acc >= 0.80
        else "green" if acc >= 0.75
        else "yellowgreen" if acc >= 0.70
        else "yellow" if acc >= 0.65
        else "orange" if acc >= 0.55
        else "red"
    )


def _pick_shipped_group(runs: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Return the most-recent ship_label group, or None if no labels exist.

    Recency is decided by the latest ``ship_marked_at`` among the runs
    in each label. Ties broken by max ``uploaded_at``.
    """
    by_label: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        label = r.get("ship_label")
        if not label:
            continue
        by_label.setdefault(label, []).append(r)
    if not by_label:
        return None

    def _label_recency(label: str) -> tuple[str, str]:
        group = by_label[label]
        marked = max((r.get("ship_marked_at") or "" for r in group), default="")
        uploaded = max((r.get("uploaded_at") or "" for r in group), default="")
        return (marked, uploaded)

    most_recent_label = max(by_label.keys(), key=_label_recency)
    return by_label[most_recent_label]


def _build_badge_payload(bench: str, runs_sorted: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compute the shields.io endpoint badge JSON for one bench.

    Strategy (in priority order):
    1. **Released-package parity** — if ``BENCH_PARITY.yaml`` has a row
       for the headline package (``astrocyte``), display its frozen
       scores with a ``v<version>`` label. This is what the README
       should show: "what `pip install astrocyte` produces".
    2. **Most-recent ship_label group** — if any run carries a
       ``ship_label``, pick the most-recent label group and mean its
       overall scores. Use this before the first release-mark.
    3. **Latest non-null overall** — fallback for pre-curation runs.
    """
    label = _BENCH_LABEL.get(bench, bench)
    bench_runs = [r for r in runs_sorted if isinstance(r.get("overall"), (int, float))]

    # Priority 1: latest release
    release = _latest_release_for_badge(bench)
    if release:
        score_body = release["scores"][bench]
        acc = float(score_body["overall"])
        n = score_body.get("n_questions")
        runs = score_body.get("runs", 1)
        pkg = release["package"]
        ver = release["version"]
        n_block = f"n={n}" if n else ""
        runs_block = f"{runs} runs" if runs and runs > 1 else ""
        parts = [p for p in (n_block, runs_block) if p]
        suffix = f" ({', '.join(parts)})" if parts else ""
        return {
            "schemaVersion": 1,
            "label": f"{label} ({pkg} v{ver}){suffix}",
            "message": f"{acc * 100:.1f}%",
            "color": _badge_color(acc),
        }

    if not bench_runs:
        return None

    shipped = _pick_shipped_group(bench_runs)
    if shipped:
        accs = [float(r["overall"]) for r in shipped]
        mean_acc = sum(accs) / len(accs)
        n = sum(int(r.get("n_questions") or 0) for r in shipped) // len(shipped)
        ship_cycle = shipped[0].get("ship_label", "")
        runs_suffix = f" × {len(shipped)} runs" if len(shipped) > 1 else ""
        sub = ship_cycle.upper() if ship_cycle else ""
        sub_block = f", {sub}" if sub else ""
        return {
            "schemaVersion": 1,
            "label": f"{label} (n={n}{sub_block}{runs_suffix})" if n else label,
            "message": f"{mean_acc * 100:.1f}%",
            "color": _badge_color(mean_acc),
        }

    # Fallback — latest non-null overall.
    latest = bench_runs[-1]
    acc = float(latest["overall"])
    n = latest.get("n_questions")
    stage = latest.get("stage") or ""
    n_block = f"n={n}" if n else ""
    parts = [p for p in (n_block, stage) if p]
    suffix = f" ({', '.join(parts)})" if parts else ""
    return {
        "schemaVersion": 1,
        "label": f"{label}{suffix}",
        "message": f"{acc * 100:.1f}%",
        "color": _badge_color(acc),
    }


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
    p.add_argument(
        "--rescan",
        action="store_true",
        help=(
            "Walk benchmark-results/ and archive every project that lacks an _ARCHIVED marker. "
            "Stage names are derived from the project directory. Idempotent — safe to re-run."
        ),
    )
    p.add_argument(
        "--rescan-root",
        type=Path,
        default=CANONICAL_RESULTS_ROOT,
        help=f"Root to walk when --rescan is set (default: {CANONICAL_RESULTS_ROOT}).",
    )
    p.add_argument(
        "--mem0-cutoff",
        default=DEFAULT_MEM0_CUTOFF,
        help=f"Mem0-harness cutoff to canonicalize on (default: {DEFAULT_MEM0_CUTOFF}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what --rescan would upload without touching R2 or writing markers.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-archive projects even when an _ARCHIVED marker is present.",
    )
    p.add_argument(
        "--include-smoke",
        action="store_true",
        help=(
            f"Archive smoke / micro runs too. By default the rescan skips "
            f"any project whose stage contains 'smoke' or whose n_questions "
            f"is below {SMOKE_MIN_QUESTIONS}."
        ),
    )
    p.add_argument(
        "--refresh-labels",
        action="store_true",
        help=(
            "Walk benchmark-results/ and patch ship_label fields into "
            "already-archived per-day manifests. Run this after "
            "`make bench-mark-shipped` to propagate label changes without "
            "re-uploading anything."
        ),
    )
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

    if args.refresh_labels:
        # Count is logged by refresh_ship_labels itself; we don't gate
        # the exit code on it because "no projects to refresh" is a
        # clean state (e.g. nothing marked shipped yet).
        await refresh_ship_labels(
            root=args.rescan_root,
            cfg=cfg,
            dry_run=args.dry_run,
        )
        return 0

    if args.rescan:
        n = await archive_rescan(
            root=args.rescan_root,
            cfg=cfg,
            mem0_cutoff=args.mem0_cutoff,
            dry_run=args.dry_run,
            force=args.force,
            include_smoke=args.include_smoke,
        )
        return 0 if n > 0 or args.dry_run else 1

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
