"""Mark a benchmark project as part of a SHIPPED cycle.

Drops ``_SHIP_LABEL.json`` into the project directory. The label is
picked up by the archive rescan (``scripts.archive_bench_results``) and
propagated into the per-day manifest. The trajectory regenerator then
groups runs by ``ship_label``, picks the most recent label group, and
computes the mean overall accuracy per bench — that's the number the
README badges display.

A typical cycle ships a single condition with 2 replicate runs. Mark
both replicate directories with the same label::

    make bench-mark-shipped PROJECT=m18b-b1-dp-rrf-run-1 LABEL=m18b
    make bench-mark-shipped PROJECT=m18b-b1-dp-rrf-run-2 LABEL=m18b

After re-running ``make bench-archive-rescan``, the trajectory
regenerator writes ``badges/<bench>.json`` reflecting the mean.

Usage::

    python -m scripts.mark_shipped --project m18b-b1-dp-rrf-run-1 --label m18b \\
        --rationale "B1-dp+RRF: dateparser Pass B + RRF fact fusion"

    # Remove a previously-applied label
    python -m scripts.mark_shipped --project m18b-b1-dp-rrf-run-1 --unmark

The ``--project`` argument matches the directory name (with or without
the ``astrocyte-`` prefix). The script finds every matching project
across both benches (LoCoMo + LongMemEval) — typical cycle-close marks
both at once because the same condition was run against both datasets.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CANONICAL_ROOT = Path("benchmark-results")
SHIP_LABEL_FILE = "_SHIP_LABEL.json"
ARCHIVED_MARKER = "_ARCHIVED"

# Label format: lowercase letters / digits / dots / dashes. Matches stage
# naming convention (m13, m14.6, m18b, etc.) so future trajectory grouping
# can sort labels chronologically.
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")


def _find_project_dirs(project_arg: str, root: Path = CANONICAL_ROOT) -> list[Path]:
    """Find every project directory matching ``project_arg``.

    Matches both ``astrocyte-<name>`` and bare ``<name>`` forms so the
    user doesn't have to remember which is which.
    """
    bare = project_arg.removeprefix("astrocyte-")
    prefixed = f"astrocyte-{bare}"
    matches: list[Path] = []
    if not root.exists():
        return matches
    for harness_dir in sorted(root.iterdir()):
        if not harness_dir.is_dir() or harness_dir.name.startswith(("_", ".")):
            continue
        for bench_dir in sorted(harness_dir.iterdir()):
            if not bench_dir.is_dir():
                continue
            for cand in (bench_dir / bare, bench_dir / prefixed):
                if cand.is_dir():
                    matches.append(cand)
    return matches


def _write_ship_label(project_dir: Path, *, label: str, rationale: str) -> None:
    body = {
        "label": label,
        "marked_at": datetime.now(timezone.utc).isoformat(),
        "rationale": rationale,
    }
    (project_dir / SHIP_LABEL_FILE).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def _remove_ship_label(project_dir: Path) -> bool:
    path = project_dir / SHIP_LABEL_FILE
    if path.exists():
        path.unlink()
        return True
    return False


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--project",
        required=True,
        help="Project directory name (with or without 'astrocyte-' prefix), e.g. 'm18b-b1-dp-rrf-run-1'.",
    )
    p.add_argument(
        "--label",
        help="Cycle label, e.g. 'm18b'. Required unless --unmark.",
    )
    p.add_argument(
        "--rationale",
        default="",
        help="One-line rationale recorded in the marker file (optional but recommended).",
    )
    p.add_argument(
        "--unmark",
        action="store_true",
        help="Remove an existing _SHIP_LABEL.json instead of writing one.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing _SHIP_LABEL.json instead of refusing.",
    )
    p.add_argument(
        "--allow-unarchived",
        action="store_true",
        help="Allow marking a project that hasn't been archived to R2 yet (default refuses).",
    )
    args = p.parse_args()

    if not args.unmark:
        if not args.label:
            print("ERROR: --label is required unless --unmark.", file=sys.stderr)
            sys.exit(2)
        if not LABEL_RE.match(args.label):
            print(f"ERROR: invalid --label {args.label!r} — must match {LABEL_RE.pattern}", file=sys.stderr)
            sys.exit(2)

    matches = _find_project_dirs(args.project)
    if not matches:
        print(f"ERROR: no project directories matching {args.project!r} under {CANONICAL_ROOT}/", file=sys.stderr)
        sys.exit(1)

    print(f"  found {len(matches)} matching project director{'y' if len(matches) == 1 else 'ies'}:")
    for d in matches:
        print(f"    {d}")

    exit_code = 0
    for project_dir in matches:
        marker = project_dir / SHIP_LABEL_FILE

        if args.unmark:
            if _remove_ship_label(project_dir):
                print(f"  removed {marker}")
            else:
                print(f"  no marker at {marker} — nothing to remove")
            continue

        if not args.allow_unarchived and not (project_dir / ARCHIVED_MARKER).exists():
            print(
                f"  REFUSE {project_dir}: no _ARCHIVED marker yet — run "
                f"`make bench-archive-rescan` first, or pass --allow-unarchived"
            )
            exit_code = 1
            continue

        if marker.exists() and not args.force:
            existing = json.loads(marker.read_text(encoding="utf-8"))
            print(
                f"  REFUSE {project_dir}: already labelled as {existing.get('label')!r} "
                f"({existing.get('marked_at')}); pass --force to overwrite"
            )
            exit_code = 1
            continue

        _write_ship_label(project_dir, label=args.label, rationale=args.rationale)
        print(f"  marked {marker} label={args.label!r}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
