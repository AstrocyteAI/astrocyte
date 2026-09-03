"""Drive AML's own answer/judge prompts, without AML's broken driver loop.

Why this module exists
----------------------

AML's published pipelines are the authority on *what* the answerer is asked
and *how* the judge labels — and we want those verbatim, since deviating
would make our numbers incomparable. But their ``answer`` and ``evaluate``
subcommands cannot run as shipped::

    async with httpx.AsyncClient(timeout=120) as client, \\
            output.open("a", encoding="utf-8") as handle:
    TypeError: '_io.TextIOWrapper' object does not support the
               asynchronous context manager protocol

``Path.open()`` returns a *synchronous* context manager, which ``async with``
rejects on every Python 3.x. The bug is in all six pipelines (9 sites:
longmemeval-s, locomo-refined, clbench, beam, scriptmem), so it is a defect
in the published driver, not something specific to our environment.

The split we take: import their ``render_answer_prompt``,
``render_accuracy_prompt`` and ``parse_judge_label`` — the parts that define
the evaluation — and supply our own loop around them. Prompt fidelity is
preserved exactly; only the broken plumbing is replaced.

Combined with :mod:`aml_selfeval.shim`, this makes the whole self-evaluation
provider-agnostic: AML's prompts, our models, no OpenAI account.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx


def load_pipeline(repo_root: str | Path, bench: str) -> ModuleType:
    """Import ``data/<bench>/pipeline.py`` from a clone of AML's repo.

    The repo root goes on ``sys.path`` because the pipelines do a bare
    ``from api_config import ...`` at module scope.
    """
    root = Path(repo_root).resolve()
    path = root / "data" / bench / "pipeline.py"
    if not path.exists():
        raise SystemExit(f"no pipeline at {path} — is --aml-repo a clone of the AML repo?")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    spec = importlib.util.spec_from_file_location(f"aml_pipeline_{bench}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def complete(
    client: httpx.AsyncClient, base_url: str, api_key: str, model: str, prompt: str,
) -> str:
    """Byte-for-byte the request AML's own ``complete()`` sends."""
    resp = await client.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


async def run_answer(pipeline: ModuleType, args: argparse.Namespace) -> None:
    items = _rows(Path(args.input))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {r["id"] for r in _rows(out)} if args.resume else set()
    todo = [i for i in items if i["id"] not in done]
    if done:
        print(f"answer: resuming, {len(done)} done, {len(todo)} to go", file=sys.stderr)

    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        # Sync file handle in its own `with`, NOT folded into the `async with`
        # above — that fold is the exact mistake that breaks AML's own driver.
        # ASYNC230: blocking writes are deliberate. Each row costs one LLM
        # round-trip; flushing incrementally is what makes a long run
        # resumable, and the write is negligible beside the network wait.
        with out.open("a" if args.resume else "w", encoding="utf-8") as handle:  # noqa: ASYNC230

            async def one(item: dict[str, Any]) -> dict[str, Any] | None:
                async with sem:
                    try:
                        text = await complete(
                            client, args.answer_base, args.answer_key, args.answer_model,
                            pipeline.render_answer_prompt(item),
                        )
                    except Exception as exc:  # noqa: BLE001 — one bad item must not kill the run
                        print(f"  !! answer {item['id']}: {exc}", file=sys.stderr)
                        return None
                    return {"id": item["id"], "generated_answer": text}

            n = 0
            for coro in asyncio.as_completed([one(i) for i in todo]):
                rec = await coro
                if rec is None:
                    continue
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
                handle.flush()
                n += 1
                if n % 10 == 0:
                    print(f"  answered {n}/{len(todo)}", file=sys.stderr)
    print(f"answers -> {out}", file=sys.stderr)


async def run_evaluate(pipeline: ModuleType, args: argparse.Namespace) -> None:
    items = {r["id"]: r for r in _rows(Path(args.input))}
    answers = {r["id"]: r["generated_answer"] for r in _rows(Path(args.output))}
    missing = set(items) - set(answers)
    if missing:
        # AML hard-fails on any mismatch; we report and judge what we have,
        # so one dropped answer does not discard an expensive run.
        print(f"warning: {len(missing)} items have no answer, skipping them", file=sys.stderr)

    out = Path(args.scored)
    out.parent.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        with out.open("w", encoding="utf-8") as handle:  # noqa: ASYNC230 — see run_answer

            async def one(ident: str) -> dict[str, Any] | None:
                async with sem:
                    prompt = pipeline.render_accuracy_prompt(items[ident], answers[ident])
                    try:
                        text = await complete(
                            client, args.judge_base, args.judge_key, args.judge_model, prompt,
                        )
                        label = pipeline.parse_judge_label(text)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  !! judge {ident}: {exc}", file=sys.stderr)
                        return None
                    return {
                        "id": ident,
                        "label": label,
                        "is_correct": label == "CORRECT",
                        "judge_response": text,
                    }

            n = 0
            for coro in asyncio.as_completed([one(i) for i in answers if i in items]):
                rec = await coro
                if rec is None:
                    continue
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
                handle.flush()
                n += 1
    print(f"scored -> {out}", file=sys.stderr)


async def run(args: argparse.Namespace) -> None:
    pipeline = load_pipeline(args.aml_repo, args.bench)
    await run_answer(pipeline, args)
    await run_evaluate(pipeline, args)


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--aml-repo", required=True, help="clone of AML-memory/agent-memory-leaderboard")
    p.add_argument("--bench", default="longmemeval-s", help="data/<bench>/pipeline.py to use")
    p.add_argument("--input", required=True, help="retrieval JSONL from `retrieve`")
    p.add_argument("--output", required=True, help="answers JSONL to write")
    p.add_argument("--scored", required=True, help="judged JSONL to write")
    p.add_argument("--answer-base", default="http://127.0.0.1:8081/v1")
    p.add_argument("--answer-model", default="")
    p.add_argument("--answer-key", default="unused")
    p.add_argument("--judge-base", default="http://127.0.0.1:8081/v1")
    p.add_argument("--judge-model", default="")
    p.add_argument("--judge-key", default="unused")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--resume", action="store_true")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="aml_selfeval.judge")
    add_arguments(p)
    asyncio.run(run(p.parse_args(argv)))


if __name__ == "__main__":
    main()
