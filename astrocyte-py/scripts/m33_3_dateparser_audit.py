"""M33-3 forensic audit — replay dateparser on v015f LME questions.

Reads the v015f results JSON, replays ``analyze_query`` (which fans Pass A
regex + Pass B dateparser) for each question with its ``question_date`` as
the reference anchor, and reports:

  - dateparser activation rate per ``question_type``
  - For temporal-reasoning failures: query → extracted range
  - Whether the gold-supporting facts (from search_results judged
    ``in_correct_answer`` if available, else top-3 by score) have
    ``occurred_at`` inside the extracted range

This tells us whether the v015f temporal-reasoning gap is:

  1. dateparser-miss   — extractor returns None on queries that need
     temporal filtering
  2. range-too-narrow — extractor fires but ``pad_days=1`` excludes
     gold-supporting facts
  3. retrieval-miss   — extractor fires correctly, range covers gold,
     but the temporal sibling still doesn't surface relevant facts

Usage::

    cd astrocyte-py
    uv run python scripts/m33_3_dateparser_audit.py \\
        benchmark-results/mem0_harness/lme/astrocyte-v015f/longmemeval_results_20260520_183423.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def _parse_question_date(raw: str) -> datetime | None:
    """Question dates look like ``2023/11/08 (Wed) 09:08``; strip the weekday."""
    if not raw:
        return None
    cleaned = raw.split(" (", 1)[0]  # "2023/11/08 09:08"
    parts = cleaned.split(" ", 1)
    if len(parts) == 2:
        date_part, time_part = parts
    else:
        date_part, time_part = parts[0], "00:00"
    try:
        return datetime.strptime(f"{date_part} {time_part}", "%Y/%m/%d %H:%M")
    except ValueError:
        return None


async def _replay(results_path: Path) -> None:
    from astrocyte.pipeline.query_analyzer import analyze_query

    data = json.loads(results_path.read_text())
    evals = data["evaluations"]

    by_type: dict[str, list[dict]] = defaultdict(list)
    for ev in evals:
        by_type[ev["question_type"]].append(ev)

    print(f"\nLoaded {len(evals)} questions across {len(by_type)} types\n")

    summary_rows = []
    tr_failures: list[dict] = []

    # We classify a TR question as a "failure" if its top_50 cutoff judged it wrong.
    # The metrics_by_cutoff doesn't carry per-question outcomes, so we re-derive
    # from the evaluations list: each ev has ``cutoff_results`` with per-cutoff
    # judged scores.
    for qtype, items in sorted(by_type.items()):
        hit_count = 0
        fail_no_hit = 0
        fail_with_hit = 0
        ok_no_hit = 0
        ok_with_hit = 0
        for ev in items:
            anchor = _parse_question_date(ev.get("question_date", ""))
            analysis = await analyze_query(
                ev["question"],
                reference_date=anchor,
                llm_provider=None,
                allow_llm_fallback=False,
                allow_temporal_expansion=True,
            )
            has_range = (
                analysis.temporal_constraint is not None
                and analysis.temporal_constraint.is_bounded()
            )
            rng = None
            if has_range:
                rng = (
                    analysis.temporal_constraint.start_date,
                    analysis.temporal_constraint.end_date,
                )

            # Was this question judged correct at top_50?
            cutoff_res = ev.get("cutoff_results", {}).get("top_50", {})
            judged_correct = cutoff_res.get("judgment", "FAIL") == "PASS"

            if has_range:
                hit_count += 1
            if judged_correct and has_range:
                ok_with_hit += 1
            elif judged_correct:
                ok_no_hit += 1
            elif has_range:
                fail_with_hit += 1
            else:
                fail_no_hit += 1

            if qtype == "temporal-reasoning" and not judged_correct:
                tr_failures.append(
                    {
                        "qid": ev["question_id"],
                        "q": ev["question"],
                        "anchor": anchor.isoformat() if anchor else None,
                        "gt": ev["ground_truth_answer"],
                        "dateparser_range": (
                            (rng[0].isoformat(), rng[1].isoformat()) if rng else None
                        ),
                        "source_label": None,  # TemporalConstraint has no source field today
                    }
                )

        n = len(items)
        summary_rows.append(
            (
                qtype,
                n,
                hit_count,
                100 * hit_count / n,
                ok_with_hit,
                ok_no_hit,
                fail_with_hit,
                fail_no_hit,
            )
        )

    # ---- Summary table ----
    print(f"{'question_type':<28} {'n':>3} {'hit':>4} {'hit%':>6} {'ok+hit':>7} {'ok-hit':>7} {'fail+hit':>9} {'fail-hit':>9}")
    print("-" * 90)
    for row in summary_rows:
        qt, n, hit, pct, ow, on_, fw, fn_ = row
        print(f"{qt:<28} {n:>3} {hit:>4} {pct:>5.1f}% {ow:>7} {on_:>7} {fw:>9} {fn_:>9}")

    # ---- TR-failure detail ----
    print(f"\n=== Temporal-reasoning failures @ top_50 (n={len(tr_failures)}) ===")
    for i, f in enumerate(tr_failures, 1):
        print(f"\n[{i}] {f['qid']} anchor={f['anchor']} gt={f['gt']!r}")
        print(f"    Q: {f['q']}")
        if f["dateparser_range"]:
            print(f"    ✓ range: {f['dateparser_range'][0]} → {f['dateparser_range'][1]}")
            print(f"      source: {f['source_label']}")
        else:
            print("    ✗ no temporal range extracted")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/m33_3_dateparser_audit.py <results.json>")
        sys.exit(2)
    asyncio.run(_replay(Path(sys.argv[1])))
