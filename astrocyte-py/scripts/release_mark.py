"""Record a released package version's bench-cycle parity in BENCH_PARITY.yaml.

Each release of ``astrocyte`` / ``astrocyte-postgres`` / ``astrocyte-gateway-py``
embeds the behavior shipped by some bench cycle (``m18b``, ``m19``, ...).
``release-mark`` appends a row to ``BENCH_PARITY.yaml`` linking the two:

    package: astrocyte
    version: "0.13.0"
    bench_cycle: m18b
    bench_tag: bench/m18b
    bench_commit: <sha of bench/m18b>
    released_at: "2026-05-18"
    scores:
      locomo:      {overall: 0.8375, n_questions: 200, runs: 2}
      longmemeval: {overall: 0.7167, n_questions: 30,  runs: 2}

The README badge writer reads this file and shows the most recent release's
scores — so the badge tracks "what `pip install astrocyte` produces" rather
than "what last night's ablation produced".

Usage::

    # Append a row (does not touch git tags)
    python -m scripts.release_mark --package astrocyte --version 0.13.0 --cycle m18b

    # Also create an annotated git tag astrocyte-v0.13.0 at HEAD
    python -m scripts.release_mark --package astrocyte --version 0.13.0 --cycle m18b --tag

    # Preview what would be written without modifying anything
    python -m scripts.release_mark --package astrocyte --version 0.13.0 --cycle m18b --dry-run

    # Overwrite an existing entry (force re-mark)
    python -m scripts.release_mark --package astrocyte --version 0.13.0 --cycle m18b --force

The cycle must already exist as an annotated ``bench/<cycle>`` git tag —
created by ``make bench-tag-shipped LABEL=<cycle>`` after marking the
shipped run pair. This refusal is intentional: a release without a
reproducible bench tag is a release that can't be retroactively verified.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

# Shared helpers — tag_shipped already implements project discovery + score reading.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.tag_shipped import (  # noqa: E402, I001
    KNOWN_BENCHES,
    _find_labelled_projects,
    _read_bench_score,
    _resolve_commit,
    _tag_exists,
    _working_tree_dirty,
)

# yaml is in the dev extras (astrocyte-py uses it for config loading).
try:
    import yaml
except ImportError as e:
    raise SystemExit(
        "ERROR: PyYAML is required (uv sync --extra dev). "
        "If you're outside the astrocyte-py venv, run via `uv run python -m scripts.release_mark`."
    ) from e

KNOWN_PACKAGES = (
    "astrocyte",
    "astrocyte-postgres",
    "astrocyte-gateway-py",
    "astrocyte-qdrant",
    "astrocyte-neo4j",
    "astrocyte-elasticsearch",
    "astrocyte-llm-litellm",
    "astrocyte-ingestion-s3",
    "astrocyte-ingestion-kafka",
    "astrocyte-ingestion-redis",
    "astrocyte-ingestion-github",
    "astrocyte-ingestion-document",
    "astrocyte-integration-tavus",
    "astrocyte-integration-llm-wrapper",
    "astrocyte-stack",
)

SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[.\-+][0-9A-Za-z.-]+)?$")
PARITY_FILE_NAME = "BENCH_PARITY.yaml"


def _find_repo_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` until finding the .git directory; return the parent."""
    cur = (start or Path(__file__).resolve()).resolve()
    for parent in (cur, *cur.parents):
        if (parent / ".git").exists() and (parent / PARITY_FILE_NAME).exists():
            return parent
    # Fallback — look for just .git (BENCH_PARITY.yaml might not exist yet)
    cur = (start or Path(__file__).resolve()).resolve()
    for parent in (cur, *cur.parents):
        if (parent / ".git").exists():
            return parent
    raise SystemExit("ERROR: not inside a git repo")


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _load_parity(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "releases": []}
    body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "releases" not in body or not isinstance(body["releases"], list):
        body["releases"] = []
    if "schema_version" not in body:
        body["schema_version"] = 1
    return body


def _save_parity(path: Path, body: dict) -> None:
    # Preserve the header comment block if the file already exists.
    existing_header = ""
    if path.exists():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        comment_block = []
        for line in lines:
            if line.lstrip().startswith("#") or line.strip() == "":
                comment_block.append(line)
            else:
                break
        existing_header = "".join(comment_block).rstrip() + "\n\n" if comment_block else ""
    rendered = yaml.dump(
        body,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=120,
    )
    path.write_text(existing_header + rendered, encoding="utf-8")


def _compute_cycle_scores(label: str) -> dict[str, dict]:
    """Locally compute mean scores per bench for the given cycle label."""
    project_dirs = _find_labelled_projects(label)
    if not project_dirs:
        raise SystemExit(
            f"ERROR: no project directories carry _SHIP_LABEL.json with label={label!r}.\n"
            f"Run `make bench-mark-shipped PROJECT=... LABEL={label}` first."
        )
    by_bench: dict[str, list[tuple[float, int | None]]] = {}
    for d in project_dirs:
        bench, acc, n = _read_bench_score(d)
        if bench not in KNOWN_BENCHES or acc is None:
            continue
        by_bench.setdefault(bench, []).append((acc, n))
    result: dict[str, dict] = {}
    for bench, pairs in by_bench.items():
        scores = [a for a, _ in pairs]
        ns = [n for _, n in pairs if n]
        result[bench] = {
            "overall": round(mean(scores), 4),
            "n_questions": ns[0] if ns and all(n == ns[0] for n in ns) else None,
            "runs": len(pairs),
        }
    return result


def _build_release_row(
    *, package: str, version: str, cycle: str, repo_root: Path
) -> dict:
    """Construct the YAML row to append."""
    if not _tag_exists(f"bench/{cycle}"):
        raise SystemExit(
            f"ERROR: git tag bench/{cycle} does not exist. "
            f"Run `make bench-tag-shipped LABEL={cycle}` first."
        )
    bench_commit = _resolve_commit(f"bench/{cycle}")
    scores = _compute_cycle_scores(cycle)
    return {
        "package": package,
        "version": str(version),
        "bench_cycle": cycle,
        "bench_tag": f"bench/{cycle}",
        "bench_commit": bench_commit,
        "released_at": datetime.now(timezone.utc).date().isoformat(),
        "scores": scores,
    }


def _release_tag_name(version: str) -> str:
    """One tag per release, not per package — projects ship the packages
    in lockstep at the same VERSION so a single ``v<VERSION>`` covers
    all of them. The tag message enumerates the packages."""
    return f"v{version}"


def _build_release_tag_message(version: str, rows: list[dict]) -> str:
    """Compose the annotated-tag message for a lockstep release covering
    multiple packages at the same VERSION. ``rows`` is the list of every
    BENCH_PARITY.yaml entry matching this VERSION (typically one per
    lockstep package; scores are the same across them)."""
    if not rows:
        raise ValueError("at least one release row required to compose tag message")

    # Cycle + scores assumed identical across lockstep rows; use the first.
    cycle = rows[0].get("bench_cycle", "?")
    commit_sha = rows[0].get("bench_commit", "?")
    lines = [
        f"Release v{version}",
        "",
        f"Bench parity: {cycle} (commit {commit_sha[:12]})",
        "",
        "Packages released:",
    ]
    for r in sorted(rows, key=lambda x: x.get("package", "")):
        lines.append(f"  {r.get('package')} v{r.get('version')}")

    scores = rows[0].get("scores") or {}
    if scores:
        lines.append("")
        lines.append("Bench scores (frozen at release time):")
        for bench, body in sorted(scores.items()):
            n = body.get("n_questions")
            n_repr = f"n={n}" if n else "n=?"
            runs = body.get("runs", 1)
            run_word = "run" if runs == 1 else "runs"
            label = {"locomo": "LoCoMo", "longmemeval": "LongMemEval"}.get(bench, bench)
            lines.append(
                f"  {label} ({n_repr}, {runs} {run_word}):  {body['overall'] * 100:.2f}%"
            )
    lines += ["", f"See BENCH_PARITY.yaml at the repo root, and the bench/{cycle} cycle tag."]
    return "\n".join(lines) + "\n"


def _existing_tag_commit(tag: str) -> str | None:
    """Return the resolved sha the tag points at, or None if missing."""
    r = _run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"], check=False)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--package", required=True, choices=KNOWN_PACKAGES, help="Released package name.")
    p.add_argument("--version", required=True, help="Released version (X.Y.Z).")
    p.add_argument("--cycle", required=True, help="Bench cycle label, e.g. 'm18b'.")
    p.add_argument(
        "--tag",
        action="store_true",
        help="Also create an annotated git tag <package>-v<version> at HEAD.",
    )
    p.add_argument(
        "--commit",
        default="HEAD",
        help="Commit-ish for the release tag (default: HEAD). Ignored without --tag.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the row + tag message without writing anything.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing (package, version) entry and/or a colliding release tag.",
    )
    p.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Create the release tag even with uncommitted changes (avoid).",
    )
    args = p.parse_args()

    if not SEMVER_RE.match(args.version):
        raise SystemExit(
            f"ERROR: --version {args.version!r} doesn't look like semver (X.Y.Z[-pre][+build])."
        )

    repo_root = _find_repo_root()
    parity_path = repo_root / PARITY_FILE_NAME

    row = _build_release_row(
        package=args.package,
        version=args.version,
        cycle=args.cycle,
        repo_root=repo_root,
    )

    print("--- release row ---")
    print(json.dumps(row, indent=2, default=str))
    print("--- end release row ---\n")

    # YAML mutation
    body = _load_parity(parity_path)
    existing_index: int | None = None
    for i, r in enumerate(body["releases"]):
        if r.get("package") == args.package and str(r.get("version")) == args.version:
            existing_index = i
            break
    if existing_index is not None and not args.force:
        raise SystemExit(
            f"ERROR: BENCH_PARITY.yaml already has {args.package} v{args.version}. "
            f"Pass --force to overwrite."
        )

    if existing_index is not None:
        body["releases"][existing_index] = row
        action = "overwriting"
    else:
        body["releases"].append(row)
        action = "appending"

    # Always sort by released_at desc, then package, for stable diffs and so the
    # badge writer's "latest" pick is deterministic.
    body["releases"].sort(
        key=lambda r: (r.get("released_at", ""), r.get("package", "")),
        reverse=True,
    )

    if args.dry_run:
        print(f"  DRY would {action} entry in {parity_path}")
    else:
        _save_parity(parity_path, body)
        print(f"  {action} entry in {parity_path}")

    # Optional release tag — one v<VERSION> tag covers ALL packages
    # released at this version (projects ship lockstep). The message
    # enumerates every BENCH_PARITY.yaml row matching this version, so
    # the tag is idempotent across the lockstep `release-mark-all` loop:
    # first call creates the tag, subsequent calls update its message
    # to include the newly-added rows (idempotent at the same commit).
    if args.tag:
        tag = _release_tag_name(args.version)
        # Reload parity (we just wrote) so the message includes the new row.
        body_after = _load_parity(parity_path) if not args.dry_run else body
        matching_rows = [
            r for r in (body_after.get("releases") or [])
            if str(r.get("version")) == args.version
        ]
        if args.dry_run:
            # The dry-run won't have persisted the new row, so include it manually.
            matching_rows = matching_rows + [row]
        tag_msg = _build_release_tag_message(args.version, matching_rows)
        print("\n--- release-tag message ---")
        print(tag_msg)
        print("--- end release-tag message ---\n")

        if args.dry_run:
            print(f"  DRY would create / update tag {tag} on {args.commit}")
            return

        if not args.allow_dirty and _working_tree_dirty():
            raise SystemExit(
                "ERROR: working tree is dirty. Tagging dirty state defeats reproducibility.\n"
                "Either commit changes first, or pass --allow-dirty if you know what you're doing."
            )

        commit_sha = _resolve_commit(args.commit)
        existing_sha = _existing_tag_commit(tag)
        if existing_sha is not None:
            if existing_sha != commit_sha and not args.force:
                raise SystemExit(
                    f"ERROR: tag {tag} already exists at {existing_sha[:12]} (would point at "
                    f"{commit_sha[:12]}). Pass --force to overwrite."
                )
            # Same commit → safe to re-tag (refreshes message with newly-added rows).
            print(f"  refreshing tag {tag} at {commit_sha[:12]} (covers {len(matching_rows)} package(s))")
            _run(["git", "tag", "-d", tag])
        else:
            print(f"  creating annotated tag {tag} -> {commit_sha[:12]}")
        _run(["git", "tag", "-a", tag, "-m", tag_msg, commit_sha])
        print("\n  done. Push when ready:")
        print(f"    git push origin {tag}")

    # Reminder — the badge writer reads BENCH_PARITY.yaml, but the public badge
    # endpoint is regenerated only when bench-refresh-labels (or rescan) runs.
    if not args.dry_run:
        print("\n  next: refresh public badges so README reflects the new release:")
        print("    make bench-refresh-labels")


if __name__ == "__main__":
    main()
