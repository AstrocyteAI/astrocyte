"""Staleness benchmark: how often does memory serve a fact the world has changed?

``docs/_design/anchored-documents.md`` §9 item 3 and the essay it answers
(critique 3: "the past is treated as truth") ask one question nobody
publishes a number for: when the thing a memory describes changes and **nobody
tells the memory system**, how often does it hand back the old value as if it
were current?

Each scenario is a real git repository of small configuration files:

1. **Commit 1.** Every file holds one fact. A conversation states each fact
   and the file it lives in, and is ingested through ``/add``.
2. **Commit 2.** Half the files change value. Nothing is said about it; no
   ``/add`` follows. This is the silent change the essay describes.
3. **Questions.** One ``/search`` per fact. The evidence returned is scored
   without a model, by exact value match:

   - *changed facts:* **stale-unflagged** when the evidence contains the old
     value, not the new one, and no returned item is flagged stale; **flagged**
     when an item carrying the old value is flagged; **current** when the new
     value is present.
   - *unchanged facts (control):* **recall** when the evidence contains the
     value. A system that "wins" by forgetting everything shows up here.

Systems receive ``context: {"repo": <path>, "head": <sha>}`` on every request,
so an anchored system (anchored-documents P1) can check the repository.
Today's systems ignore it: the baseline numbers are what memory does without
anchors. An item is flagged stale by a truthy ``stale`` field, or by the text
"may be stale" in its content.

Values are random tokens, never real defaults, so no model can guess the new
value from priors. Nothing here is specific to Astrocyte: any service with the
AML Add/Search contract can be scored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# (file, setting, question, value kind). Kinds pick a value generator below.
_TEMPLATES: list[tuple[str, str, str, str]] = [
    (
        "config/retry.yaml",
        "max_retries",
        "What is the maximum number of retries?",
        "int",
    ),
    ("config/server.yaml", "port", "Which port does the server listen on?", "port"),
    ("config/cache.yaml", "ttl_seconds", "What is the cache TTL in seconds?", "int"),
    (
        "deploy/region.txt",
        "primary_region",
        "What is the primary deployment region?",
        "region",
    ),
    (
        "config/db.yaml",
        "pool_size",
        "What is the database connection pool size?",
        "int",
    ),
    (
        "config/limits.yaml",
        "rate_limit_per_minute",
        "What is the per-minute rate limit?",
        "int",
    ),
    ("config/queue.yaml", "queue_name", "What is the name of the job queue?", "name"),
    (
        "config/auth.yaml",
        "token_ttl_minutes",
        "How many minutes does an auth token last?",
        "int",
    ),
    ("config/storage.yaml", "bucket", "Which storage bucket holds uploads?", "name"),
    (
        "config/batch.yaml",
        "batch_size",
        "What batch size does the importer use?",
        "int",
    ),
    (
        "config/timeouts.yaml",
        "request_timeout_ms",
        "What is the request timeout in milliseconds?",
        "int",
    ),
    (
        "config/flags.yaml",
        "release_channel",
        "Which release channel is configured?",
        "name",
    ),
]


def _value(kind: str, rng: random.Random) -> str:
    if kind == "port":
        return str(rng.randint(20000, 64000))
    if kind == "region":
        return f"{rng.choice(['zx', 'qv', 'kp', 'wj'])}-{rng.choice(['north', 'south', 'east', 'west'])}-{rng.randint(10, 99)}"
    if kind == "name":
        return f"{rng.choice(['amber', 'cobalt', 'indigo', 'saffron', 'teal'])}-{rng.randint(1000, 9999)}"
    return str(rng.randint(1000, 99999))


@dataclass
class Fact:
    file: str
    setting: str
    question: str
    old: str
    new: str | None  # None: unchanged (control)


def make_facts(n: int, seed: int) -> list[Fact]:
    """``n`` facts, half of them changed in commit 2. Deterministic in ``seed``."""
    rng = random.Random(seed)
    facts: list[Fact] = []
    used: list[str] = []

    def fresh(kind: str) -> str:
        # Scoring matches values as substrings, so no value may contain, or be
        # contained in, any other value of the run.
        while True:
            v = _value(kind, rng)
            if all(v not in u and u not in v for u in used):
                used.append(v)
                return v

    for i in range(n):
        file, setting, question, kind = _TEMPLATES[i % len(_TEMPLATES)]
        if i >= len(_TEMPLATES):  # distinct files and questions beyond one round
            stem, ext = file.rsplit(".", 1)
            file = f"{stem}_{i // len(_TEMPLATES)}.{ext}"
            question = f"{question[:-1]} in {file}?"
        old = fresh(kind)
        new = fresh(kind)
        facts.append(Fact(file, setting, question, old, new if i % 2 == 0 else None))
    return facts


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def build_repo(root: Path, facts: list[Fact]) -> tuple[str, str]:
    """Write commit 1 (old values) and commit 2 (changed values). Returns both SHAs."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "staleness@bench.local")
    _git(root, "config", "user.name", "staleness bench")

    def write(values: dict[str, str]) -> None:
        for f in facts:
            path = root / f.file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{f.setting}: {values[f.file]}\n", encoding="utf-8")

    write({f.file: f.old for f in facts})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial configuration")
    first = _git(root, "rev-parse", "HEAD")
    write({f.file: f.new or f.old for f in facts})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "update configuration")
    return first, _git(root, "rev-parse", "HEAD")


def conversation(facts: list[Fact], at: datetime) -> list[dict[str, Any]]:
    """One user/assistant exchange per fact, all at commit-1 time."""
    ms = int(at.timestamp() * 1000)
    turns: list[dict[str, Any]] = []
    for i, f in enumerate(facts):
        turns.append(
            {
                "role": "user",
                "content": f"I set {f.setting} to {f.old} in {f.file}.",
                "timestamp": ms + 2 * i * 1000,
            }
        )
        turns.append(
            {
                "role": "assistant",
                "content": f"Noted: {f.setting} is {f.old} ({f.file}).",
                "timestamp": ms + (2 * i + 1) * 1000,
            }
        )
    return turns


def _flagged(item: dict[str, Any]) -> bool:
    return (
        bool(item.get("stale"))
        or "may be stale" in str(item.get("content", "")).lower()
    )


def score_fact(fact: Fact, items: list[dict[str, Any]]) -> str:
    """One of: current, flagged, stale_unflagged, missing (changed facts);
    recalled, missing (controls)."""
    texts = [str(i.get("content", "")) for i in items]
    if fact.new is None:
        return "recalled" if any(fact.old in t for t in texts) else "missing"
    if any(fact.new in t for t in texts):
        return "current"
    carrying_old = [i for i, t in zip(items, texts) if fact.old in t]
    if not carrying_old:
        return "missing"
    return "flagged" if any(_flagged(i) for i in carrying_old) else "stale_unflagged"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    changed = [r for r in results if r["changed"]]
    controls = [r for r in results if not r["changed"]]

    def rate(rows: list[dict[str, Any]], outcome: str) -> dict[str, Any]:
        k = sum(r["outcome"] == outcome for r in rows)
        lo, hi = wilson(k, len(rows))
        return {
            "k": k,
            "n": len(rows),
            "rate": k / len(rows) if rows else 0.0,
            "ci95": [lo, hi],
        }

    return {
        "changed": {
            o: rate(changed, o)
            for o in ("stale_unflagged", "flagged", "current", "missing")
        },
        "control": {o: rate(controls, o) for o in ("recalled", "missing")},
    }


async def run(
    base_url: str,
    *,
    n: int,
    seed: int,
    run_id: str,
    workdir: Path,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 600.0,
) -> dict[str, Any]:
    facts = make_facts(n, seed)
    repo = workdir / f"repo-{run_id}"
    first, head = build_repo(repo, facts)
    bank = f"staleness:{run_id}"
    async with httpx.AsyncClient(
        base_url=base_url, transport=transport, timeout=timeout
    ) as client:
        turns = conversation(facts, datetime(2024, 3, 1, tzinfo=UTC))
        for start in range(0, len(turns), 20):  # small batches, like real sessions
            r = await client.post(
                "/add",
                json={
                    "request_id": f"{run_id}:{start}",
                    "user_id": bank,
                    "session_id": f"{run_id}:s1",
                    "messages": turns[start : start + 20],
                    "context": {"repo": str(repo), "head": first},
                },
            )
            r.raise_for_status()
        results: list[dict[str, Any]] = []
        for f in facts:  # commit 2 is now HEAD; nothing told the memory system
            r = await client.post(
                "/search",
                json={
                    "query": f.question,
                    "user_id": bank,
                    "top_k": 50,
                    "context": {"repo": str(repo), "head": head},
                },
            )
            r.raise_for_status()
            items = r.json().get("data", [])
            results.append(
                {
                    **asdict(f),
                    "changed": f.new is not None,
                    "outcome": score_fact(f, items),
                    "n_items": len(items),
                }
            )
    return {
        "run_id": run_id,
        "seed": seed,
        "n": n,
        "summary": summarize(results),
        "results": results,
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", default="http://127.0.0.1:8080")
    p.add_argument("--n", type=int, default=24)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--run-id", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="staleness-") as tmp:
        report = asyncio.run(
            run(
                args.base_url,
                n=args.n,
                seed=args.seed,
                run_id=args.run_id,
                workdir=Path(tmp),
            )
        )
    Path(args.output).write_text(json.dumps(report, indent=1), encoding="utf-8")
    json.dump(report["summary"], sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
