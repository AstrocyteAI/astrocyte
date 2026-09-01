"""AML self-evaluation — the retrieval half AML does not publish.

AML's public pipelines (``data/<bench>/pipeline.py`` in
github.com/AML-memory/agent-memory-leaderboard) ship the **answer** and
**judge** halves only: they consume an ``--input`` JSONL whose records
already contain retrieved memories. The retrieval half runs on AML's
orchestrator, which calls each participant's ``Add``/``Search``.

This module is that missing half, run locally against our own adapter:

    ingest (Add) → retrieve (Search) → emit AML-shaped input JSONL
                 → [their pipeline answer] → [their pipeline evaluate]

Why bother, when we could just run our own bench? Because the numbers
this produces are directly comparable to the leaderboard in the ways
that matter: AML's *answer prompt* and *judge rubric* are used verbatim,
and retrieval goes through the same ``/add`` + ``/search`` contract the
platform will exercise. What it cannot replicate is AML's private
answerer/judge model choice and the four benchmarks whose data they do
not publish — so treat the result as directional, not as a predicted
leaderboard placement.

Usage
-----

    # 1. Retrieve (this module) — writes AML-shaped input JSONL
    python -m aml_selfeval.retrieve \\
        --dataset longmemeval \\
        --source ../../astrocyte-py/datasets/longmemeval/longmemeval_s_cleaned.json \\
        --output runs/lme_input.jsonl --limit 90

    # 2. Answer + judge with AML's own prompts (their repo, our models)
    export ANSWER_API_BASE=... ANSWER_MODEL=... JUDGE_API_BASE=... JUDGE_MODEL=...
    python data/longmemeval-s/pipeline.py answer \\
        --input runs/lme_input.jsonl --output runs/lme_answers.jsonl
    python data/longmemeval-s/pipeline.py evaluate \\
        --input runs/lme_input.jsonl --answers runs/lme_answers.jsonl \\
        --output runs/lme_scored.jsonl

    # 3. Score
    python -m aml_selfeval.retrieve score --scored runs/lme_scored.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

# AML's formal evaluations request top_k=100; the adapter applies its own
# cap (ASTROCYTE_AML_RESULT_CAP) on top, mirroring a real submission.
AML_TOP_K = 100

# Add batching mirrors AML's documented contract: at most 20 messages or
# 2,000 words per request, split on message boundaries.
MAX_MESSAGES_PER_ADD = 20
MAX_WORDS_PER_ADD = 2000


# ── Dataset loaders ──────────────────────────────────────────────────────
#
# Each loader yields a normalized shape:
#   {id, question, gold_answer, sessions: [[{role, content, timestamp?}]]}
# so the retrieval driver stays dataset-agnostic. Only datasets we can
# legally obtain are supported; AML's suite also includes personamem,
# clbench, scriptmem, and beam, whose data they do not publish.


def load_longmemeval(path: Path) -> list[dict[str, Any]]:
    """LongMemEval-S: haystack_sessions + question + answer."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = []
    for rec in raw:
        sessions: list[list[dict[str, Any]]] = []
        for sess in rec.get("haystack_sessions", []):
            turns = [
                {"role": t.get("role", "user"), "content": t.get("content", "")}
                for t in sess
                if t.get("content")
            ]
            if turns:
                sessions.append(turns)
        items.append({
            "id": rec.get("question_id") or rec.get("id"),
            "question": rec["question"],
            "gold_answer": rec.get("answer", ""),
            "question_type": rec.get("question_type"),
            "sessions": sessions,
        })
    return items


def load_locomo(path: Path) -> list[dict[str, Any]]:
    """LoCoMo: conversation sessions + QA pairs (one item per question)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = []
    for conv_idx, conv in enumerate(raw):
        sessions: list[list[dict[str, Any]]] = []
        convo = conv.get("conversation", {})
        for key in sorted(k for k in convo if k.startswith("session_") and "date" not in k):
            turns = [
                {"role": t.get("speaker", "user"), "content": t.get("text", "")}
                for t in convo[key]
                if t.get("text")
            ]
            if turns:
                sessions.append(turns)
        for q_idx, qa in enumerate(conv.get("qa", [])):
            if "answer" not in qa:
                continue  # adversarial/unanswerable rows carry no gold
            items.append({
                "id": f"conv{conv_idx}-q{q_idx}",
                "question": qa["question"],
                "gold_answer": str(qa["answer"]),
                "question_type": qa.get("category"),
                "sessions": sessions,
            })
    return items


LOADERS = {"longmemeval": load_longmemeval, "locomo": load_locomo}


# ── Add batching (mirrors the AML contract) ──────────────────────────────


def batch_messages(turns: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a session into Add-sized batches at message boundaries."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    words = 0
    for turn in turns:
        n = len(str(turn.get("content", "")).split())
        over_count = len(current) >= MAX_MESSAGES_PER_ADD
        over_words = current and words + n > MAX_WORDS_PER_ADD
        if over_count or over_words:
            batches.append(current)
            current, words = [], 0
        current.append(turn)
        words += n
    if current:
        batches.append(current)
    return batches


# ── Driver ───────────────────────────────────────────────────────────────


async def ingest_item(
    client: httpx.AsyncClient, base: str, run_id: str, item: dict[str, Any],
) -> int:
    """Add every session of one item under its own user_id scope.

    user_id is per-item so each question sees only its own history —
    the same isolation AML enforces ("do not share or retrieve evaluation
    memories across user IDs, tasks, samples").
    """
    user_id = f"selfeval:{run_id}:{item['id']}"
    sent = 0
    for s_idx, turns in enumerate(item["sessions"]):
        session_id = f"{user_id}:s{s_idx}"
        for b_idx, batch in enumerate(batch_messages(turns)):
            resp = await client.post(f"{base}/add", json={
                "request_id": f"{session_id}:c{b_idx}",
                "messages": batch,
                "user_id": user_id,
                "session_id": session_id,
            })
            resp.raise_for_status()
            body = resp.json()
            if not body.get("success"):
                raise RuntimeError(f"add did not succeed: {body}")
            sent += 1
    return sent


async def search_item(
    client: httpx.AsyncClient, base: str, run_id: str, item: dict[str, Any],
) -> list[dict[str, Any]]:
    resp = await client.post(f"{base}/search", json={
        "query": item["question"],
        "user_id": f"selfeval:{run_id}:{item['id']}",
        "top_k": AML_TOP_K,
    })
    resp.raise_for_status()
    return resp.json().get("data", [])


def to_aml_record(item: dict[str, Any], hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Emit the record shape AML's answer pipeline consumes.

    ``render_answer_prompt`` reads ``speaker_1_memories`` and falls back to
    ``retrieved_context``/``memories``; we populate the fallback key so a
    single flat memory list renders correctly, and keep ``question`` /
    ``gold_answer`` under the names ``gold_answer()`` accepts.
    """
    lines = [f"- {h.get('content', '')}" for h in hits if h.get("content")]
    return {
        "id": item["id"],
        "question": item["question"],
        "gold_answer": item["gold_answer"],
        "question_type": item.get("question_type"),
        "retrieved_context": "\n".join(lines) if lines else "(no memories retrieved)",
        "n_retrieved": len(lines),
    }


async def run(args: argparse.Namespace) -> None:
    loader = LOADERS[args.dataset]
    items = loader(Path(args.source))
    if args.limit:
        items = items[: args.limit]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists() and args.resume:
        with out.open(encoding="utf-8") as fh:  # noqa: ASYNC230 — CLI startup, see below
            done = {json.loads(line)["id"] for line in fh if line.strip()}
        print(f"resume: {len(done)} already retrieved", file=sys.stderr)

    sem = asyncio.Semaphore(args.concurrency)
    written = 0

    # ASYNC230: blocking file IO is deliberate here — this is a CLI writing
    # newline-delimited JSON incrementally so a long run is resumable after a
    # crash. Retrieval is network-bound; the write is negligible beside it, and
    # an aiofiles dependency would buy nothing.
    async with httpx.AsyncClient(timeout=args.timeout) as client, \
            out.open("a" if args.resume else "w", encoding="utf-8") as handle:  # noqa: ASYNC230

        async def one(item: dict[str, Any]) -> dict[str, Any] | None:
            if item["id"] in done:
                return None
            async with sem:
                try:
                    await ingest_item(client, args.base_url, args.run_id, item)
                    hits = await search_item(client, args.base_url, args.run_id, item)
                except Exception as exc:  # noqa: BLE001 — one bad item must not kill the run
                    print(f"  !! {item['id']}: {exc}", file=sys.stderr)
                    return None
                return to_aml_record(item, hits)

        for coro in asyncio.as_completed([one(i) for i in items]):
            rec = await coro
            if rec is None:
                continue
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
            handle.flush()
            written += 1
            if written % 10 == 0:
                print(f"  retrieved {written}/{len(items)}", file=sys.stderr)

    print(f"wrote {written} records -> {out}", file=sys.stderr)


def score(args: argparse.Namespace) -> None:
    """Summarize AML's evaluate output: overall + per question_type."""
    with Path(args.scored).open(encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    if not rows:
        raise SystemExit("no scored rows")
    types: dict[str, list[bool]] = {}
    if args.input:
        with Path(args.input).open(encoding="utf-8") as fh:
            meta = {
                json.loads(line)["id"]: json.loads(line).get("question_type")
                for line in fh if line.strip()
            }
        for r in rows:
            types.setdefault(meta.get(r["id"]) or "unknown", []).append(bool(r["is_correct"]))

    correct = sum(1 for r in rows if r["is_correct"])
    print(f"overall: {correct}/{len(rows)} = {100.0 * correct / len(rows):.2f}%")
    for qtype, vals in sorted(types.items()):
        print(f"  {qtype:32s} {sum(vals):3d}/{len(vals):3d} = {100.0 * sum(vals) / len(vals):5.1f}%")


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="aml_selfeval.retrieve")
    sub = root.add_subparsers(dest="command", required=True)

    r = sub.add_parser("retrieve", help="drive Add/Search, emit AML input JSONL")
    r.add_argument("--dataset", required=True, choices=sorted(LOADERS))
    r.add_argument("--source", required=True, help="path to the dataset JSON")
    r.add_argument("--output", required=True)
    r.add_argument("--base-url", default="http://127.0.0.1:8080")
    r.add_argument("--run-id", default="r1")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--timeout", type=float, default=600.0)
    r.add_argument("--resume", action="store_true")

    s = sub.add_parser("score", help="summarize AML evaluate output")
    s.add_argument("--scored", required=True)
    s.add_argument("--input", default=None, help="retrieval JSONL, for per-type breakdown")

    return root


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "retrieve":
        asyncio.run(run(args))
    else:
        score(args)


if __name__ == "__main__":
    main()
