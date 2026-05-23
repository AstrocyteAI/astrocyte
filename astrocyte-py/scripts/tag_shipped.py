"""Create an annotated git tag anchoring a SHIPPED bench cycle.

After ``mark_shipped`` flags the cycle's run pair and ``refresh_labels``
publishes their badges to R2, ``tag_shipped`` creates a permanent
``bench/<label>`` git tag whose message captures the cycle's headline
scores + rationale + the projects that ship-gated it.

The tag means the cycle's number is no longer just a docs claim — it's
a checkoutable reproducible point in history. A future operator can::

    git checkout bench/m18b   # exact code state that produced 83.75%
    make bench-locomo         # re-verify the score

Usage::

    # Build + apply tag from labelled archive data
    python -m scripts.tag_shipped --label m18b

    # Tag a specific commit (retroactive case where HEAD has drifted)
    python -m scripts.tag_shipped --label m18b --commit a1b2c3d

    # Overwrite an existing tag (rare; explicit)
    python -m scripts.tag_shipped --label m18b --force

    # Print the tag message without creating the tag (preview)
    python -m scripts.tag_shipped --label m18b --dry-run

The script refuses if the working tree is dirty, unless ``--allow-dirty``
is passed. Tags pointing at dirty state defeat the reproducibility
goal of having them at all.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from statistics import mean
from typing import Any

CANONICAL_ROOT = Path("benchmark-results")
SHIP_LABEL_FILE = "_SHIP_LABEL.json"
ARCHIVED_MARKER = "_ARCHIVED"

KNOWN_BENCHES = ("locomo", "longmemeval")
_BENCH_LABEL = {"locomo": "LoCoMo", "longmemeval": "LongMemEval"}

LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")

# Where the public badges live — referenced in the tag message so a
# reader can curl the badge JSON to recover the score.
_PUBLIC_BADGE_URL_TMPL = "https://pub-fd2a5bf01e5b443085a14aedb49c4206.r2.dev/badges/{bench}.json"


def _run(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def _working_tree_dirty() -> bool:
    """True if any tracked file has uncommitted changes or untracked files exist."""
    r = _run(["git", "status", "--porcelain"], check=False)
    return bool(r.stdout.strip())


def _tag_exists(tag: str) -> bool:
    r = _run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], check=False)
    return r.returncode == 0


def _resolve_commit(commit: str) -> str:
    """Resolve a commit-ish to its full sha. Raises if not found."""
    r = _run(["git", "rev-parse", "--verify", f"{commit}^{{commit}}"], check=False)
    if r.returncode != 0:
        raise SystemExit(f"ERROR: cannot resolve commit {commit!r}: {r.stderr.strip()}")
    return r.stdout.strip()


def _find_labelled_projects(label: str, root: Path = CANONICAL_ROOT) -> list[Path]:
    """Return every project directory whose _SHIP_LABEL.json carries the label."""
    matches: list[Path] = []
    if not root.exists():
        return matches
    for marker_path in root.rglob(SHIP_LABEL_FILE):
        try:
            body = json.loads(marker_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if body.get("label") == label:
            matches.append(marker_path.parent)
    return matches


def _read_bench_score(project_dir: Path) -> tuple[str | None, float | None, int | None]:
    """Pick the most recent result JSON in the project dir and extract bench + overall + n."""
    candidates = sorted(project_dir.glob("*_results_*.json")) + sorted(project_dir.glob("results-*.json"))
    if not candidates:
        return None, None, None
    chosen = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(chosen.read_text(encoding="utf-8"))
    except Exception:
        return None, None, None
    # Mem0-harness schema
    meta = data.get("metadata") or {}
    cutoffs = data.get("metrics_by_cutoff") or {}
    if cutoffs:
        # Cutoff priority list — M35 (v0.15.0) migrated the bench harness
        # from item-count cutoffs (``top_N``) to token-budget cutoffs
        # (``max_tokens_N``). The new ship-floor convention picked at
        # M44 (v0.15.0 close) anchors on ``max_tokens_8192`` — see
        # ``docs/_design/v0.15.0-ship-decision.md`` Appendix A. Try the
        # new ship-floor first; fall back to the legacy ``top_20`` so
        # pre-M35 result JSONs (m18b, m19a, m30c, ...) still parse for
        # their BENCH_PARITY rows.
        section: dict[str, Any] = {}
        for cutoff_name in ("max_tokens_8192", "top_20"):
            section = (cutoffs.get(cutoff_name) or {}).get("overall") or {}
            if section:
                break
        if section:
            acc = section.get("accuracy")
            n = section.get("total")
            bench = meta.get("benchmark")
            return _normalize_bench(bench), (acc / 100 if acc is not None else None), n
    # PageIndex schema
    if "overall_accuracy" in data:
        # bench inferred from path (.../mem0_harness/<bench>/<project>/) or filename
        parts = project_dir.parts
        bench = next((p for p in reversed(parts) if p in KNOWN_BENCHES), None)
        return bench, float(data["overall_accuracy"]), data.get("evaluated_questions")
    return None, None, None


def _normalize_bench(name: str | None) -> str | None:
    if not name:
        return None
    n = name.lower().strip()
    if n in ("lme", "longmemeval", "long_memeval", "long-mem-eval"):
        return "longmemeval"
    if n == "locomo":
        return "locomo"
    return n


def _releases_referencing_cycle(label: str) -> list[dict[str, Any]]:
    """Return BENCH_PARITY.yaml entries whose bench_cycle == label."""
    # Walk up from this file to find BENCH_PARITY.yaml.
    cur = Path(__file__).resolve()
    parity_path: Path | None = None
    for parent in (cur, *cur.parents):
        cand = parent / "BENCH_PARITY.yaml"
        if cand.exists():
            parity_path = cand
            break
    if parity_path is None:
        return []
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return []
    try:
        body = yaml.safe_load(parity_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    releases = body.get("releases") or []
    return [r for r in releases if r.get("bench_cycle") == label]


def _read_rationale(project_dirs: list[Path]) -> str:
    """The first non-empty rationale among the labelled projects."""
    for d in project_dirs:
        path = d / SHIP_LABEL_FILE
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rationale = (body.get("rationale") or "").strip()
        if rationale:
            return rationale
    return ""


def _compose_tag_message(label: str, project_dirs: list[Path]) -> str:
    """Build the annotated-tag message for a cycle close."""
    by_bench: dict[str, list[tuple[Path, float, int | None]]] = {}
    for d in project_dirs:
        bench, acc, n = _read_bench_score(d)
        if bench is None or acc is None:
            continue
        by_bench.setdefault(bench, []).append((d, acc, n))

    rationale = _read_rationale(project_dirs)

    lines: list[str] = [f"Cycle close: {label}", ""]
    if rationale:
        lines += ["Rationale:", f"  {rationale}", ""]

    if by_bench:
        lines.append("Bench scores (mean of shipped run set):")
        for bench in sorted(by_bench):
            runs = by_bench[bench]
            mean_acc = mean(a for _, a, _ in runs)
            ns = [n for _, _, n in runs if n]
            n_repr = f"n={ns[0]}" if ns and all(n == ns[0] for n in ns) else f"n={','.join(str(n) for n in ns)}" if ns else "n=?"
            label_display = _BENCH_LABEL.get(bench, bench)
            run_word = "run" if len(runs) == 1 else "runs"
            lines.append(f"  {label_display} ({n_repr}, {len(runs)} {run_word}):  {mean_acc * 100:.2f}%")
        lines.append("")

    # The same project name typically exists under both bench dirs
    # (.../lme/<name>/ and .../locomo/<name>/) — dedupe by display name.
    lines.append("Shipped runs:")
    seen_names: set[str] = set()
    for d in sorted(project_dirs, key=lambda p: p.name):
        name = d.name.removeprefix("astrocyte-")
        if name in seen_names:
            continue
        seen_names.add(name)
        lines.append(f"  {name}")
    lines.append("")

    lines.append("R2 badges:")
    for bench in sorted(by_bench):
        lines.append(f"  {_PUBLIC_BADGE_URL_TMPL.format(bench=bench)}")

    # Released-as block — populated when BENCH_PARITY.yaml has releases
    # referencing this cycle. Re-run `make bench-tag-shipped LABEL=<...> FORCE=1`
    # after each release-mark to refresh this block.
    releases = _releases_referencing_cycle(label)
    if releases:
        lines.append("")
        lines.append("Released as:")
        for r in sorted(releases, key=lambda x: (x.get("released_at", ""), x.get("package", ""))):
            pkg = r.get("package")
            ver = r.get("version")
            day = r.get("released_at", "?")
            lines.append(f"  {pkg} v{ver}  ({day})")

    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--label", required=True, help="Cycle label, e.g. 'm18b'.")
    p.add_argument(
        "--commit",
        default="HEAD",
        help="Commit-ish to tag (default: HEAD). Use when retroactively marking a cycle whose code has drifted.",
    )
    p.add_argument(
        "--tag-prefix",
        default="bench",
        help="Tag namespace prefix (default: 'bench'); the full tag becomes <prefix>/<label>.",
    )
    p.add_argument("--dry-run", action="store_true", help="Print the tag message without creating the tag.")
    p.add_argument("--force", action="store_true", help="Overwrite an existing tag with the same name.")
    p.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Create the tag even with uncommitted changes (defeats reproducibility — avoid).",
    )
    args = p.parse_args()

    if not LABEL_RE.match(args.label):
        print(f"ERROR: invalid --label {args.label!r} — must match {LABEL_RE.pattern}", file=sys.stderr)
        sys.exit(2)

    tag = f"{args.tag_prefix}/{args.label}"

    project_dirs = _find_labelled_projects(args.label)
    if not project_dirs:
        print(
            f"ERROR: no project directories carry _SHIP_LABEL.json with label={args.label!r}.\n"
            "Run `make bench-mark-shipped PROJECT=... LABEL={args.label}` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"  found {len(project_dirs)} labelled project director{'y' if len(project_dirs) == 1 else 'ies'}:")
    for d in project_dirs:
        print(f"    {d}")

    message = _compose_tag_message(args.label, project_dirs)
    print("\n--- tag message ---")
    print(message)
    print("--- end tag message ---\n")

    if args.dry_run:
        print(f"  DRY would create tag {tag} on {args.commit}")
        return

    if not args.allow_dirty and _working_tree_dirty():
        print(
            "ERROR: working tree is dirty. Tagging dirty state defeats reproducibility.\n"
            "Either commit / stash changes first, or pass --allow-dirty if you know what you're doing.",
            file=sys.stderr,
        )
        sys.exit(1)

    commit_sha = _resolve_commit(args.commit)

    if _tag_exists(tag):
        if not args.force:
            print(
                f"ERROR: tag {tag} already exists. Pass --force to overwrite.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  deleting existing tag {tag}")
        _run(["git", "tag", "-d", tag])

    print(f"  creating annotated tag {tag} -> {commit_sha[:12]}")
    _run(["git", "tag", "-a", tag, "-m", message, commit_sha])

    print("\n  done. Push when ready:")
    print(f"    git push origin {tag}")


if __name__ == "__main__":
    main()
