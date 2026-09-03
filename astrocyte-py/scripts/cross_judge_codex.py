"""Cross-vendor judge check: re-grade a completed LME run with gpt-5.5 via Codex CLI.

Isolates the judge variable. Uses the harness's own JUDGE_PROMPT verbatim and the
already-generated answers, so the ONLY difference from the original scoring is
which model renders the verdict.

    uv run python scripts/cross_judge_codex.py <results_dir> [--cutoff max_tokens_8192]
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import sys
import tempfile
from collections import Counter

from benchmarks.longmemeval.prompts import get_judge_prompt

CODEX_CWD = tempfile.mkdtemp(prefix="codex-judge-")


def parse_verdict(raw: str) -> bool | None:
    """Harness-compatible yes/no extraction; None when unparseable."""
    after = re.split(r"</judge_thinking>|</thinking>", raw, flags=re.IGNORECASE)
    region = (after[-1] if after else raw).strip()
    for line in reversed([ln.strip().lower() for ln in region.splitlines() if ln.strip()]):
        if line in ("yes", "no"):
            return line == "yes"
    toks = re.findall(r"\b(yes|no)\b", region.lower())
    return (toks[-1] == "yes") if toks else None


async def judge_one(sem, prompt: str, timeout: float = 180.0) -> str:
    async with sem:
        proc = await asyncio.create_subprocess_exec(
            "codex", "exec", "--skip-git-repo-check", "-m", "gpt-5.5", "-",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=CODEX_CWD,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ""
        if proc.returncode != 0:
            return ""
        return out.decode("utf-8", "replace")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("--cutoff", default="max_tokens_8192")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    files = sorted(f for f in glob.glob(os.path.join(a.results_dir, "predicted_*", "*.json"))
                   if "_ingestion" not in f)
    if not files:
        print(f"no result files under {a.results_dir}", file=sys.stderr)
        return 1

    items = []
    for f in files:
        with open(f) as fh:
            d = json.load(fh)
        cr = (d.get("cutoff_results") or {}).get(a.cutoff) or {}
        gen, orig = cr.get("generated_answer"), cr.get("judgment")
        if not gen or not orig:
            continue
        items.append({
            "qid": d.get("question_id"), "qtype": d.get("question_type", "?"),
            "orig": orig,
            "prompt": get_judge_prompt(
                question_type=d.get("question_type", ""), question_id=str(d.get("question_id")),
                question=d.get("question", ""), answer=str(d.get("ground_truth_answer")),
                response=gen, question_date=d.get("question_date", "") or ""),
        })
    print(f"re-judging {len(items)} answers @ {a.cutoff} with gpt-5.5 (concurrency {a.concurrency})...")

    sem = asyncio.Semaphore(a.concurrency)
    raws = await asyncio.gather(*(judge_one(sem, it["prompt"]) for it in items))

    agree = Counter()
    orig_c = Counter()
    new_c = Counter()
    bytype = {}
    rows = []
    for it, raw in zip(items, raws):
        v = parse_verdict(raw)
        new = "UNPARSED" if v is None else ("PASS" if v else "FAIL")
        orig_c[it["orig"]] += 1
        new_c[new] += 1
        agree["same" if new == it["orig"] else "differ"] += 1
        bytype.setdefault(it["qtype"], [0, 0, 0])
        bytype[it["qtype"]][0] += it["orig"] == "PASS"
        bytype[it["qtype"]][1] += new == "PASS"
        bytype[it["qtype"]][2] += 1
        rows.append({"qid": it["qid"], "qtype": it["qtype"], "haiku": it["orig"], "gpt55": new})

    n = len(items)
    op, np_ = orig_c["PASS"], new_c["PASS"]
    print(f"\n{'':32}{'Haiku judge':>14}{'gpt-5.5 judge':>16}")
    print(f"{'PASS':32}{op:>10}/{n}{np_:>12}/{n}")
    print(f"{'score':32}{op/n*100:>13.1f}%{np_/n*100:>15.1f}%")
    print(f"\nagreement: {agree['same']}/{n} = {agree['same']/n*100:.1f}%   "
          f"delta: {(np_-op)/n*100:+.1f}pp   unparsed: {new_c['UNPARSED']}")
    print(f"\n{'type':30}{'haiku':>8}{'gpt5.5':>8}{'n':>5}")
    for t, (o, nn, c) in sorted(bytype.items()):
        print(f"  {t:28}{o:>8}{nn:>8}{c:>5}")
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(rows, fh, indent=2)
        print(f"\nper-question verdicts -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
