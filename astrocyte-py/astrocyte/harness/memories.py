"""``astrocyte memory`` — see and remove what automatic memory captured.

Capture is automatic, so inspection has to be easy: without it the only way
to check what an agent will be reminded of, or to take back something said
in passing, is to open the database by hand. Every command defaults to the
bank of the project you are standing in — the same bank the hooks and the MCP
server use — so ``astrocyte memory`` answers "what does my agent remember
about this repo?".

Search runs the agent daemon's recall path (no LLM query expansion), so it
costs nothing and returns what a prompt would be matched against.

``forget`` erases. The core forget is a soft delete (history stays visible to
``as_of`` recall), which would leave a removed key's text in the database
file; where the store can purge, the forgotten rows are then erased from disk.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import config_path
from .project import project_bank

LIST_DEFAULT = 20
SEARCH_DEFAULT = 8
MIN_PREFIX = 4  # shortest id prefix `forget` accepts


def open_local(cfg_path: Path) -> tuple[Any, Any]:
    """``(pipeline, brain)`` for the local config, wired the way the agent
    daemon needs it: no LLM call on recall, none in the background."""
    from astrocyte import Astrocyte
    from astrocyte.config import load_config
    from astrocyte.wiring import build_pipeline

    config = load_config(str(cfg_path))
    # Recall-time query expansion calls the LLM (5–9 s via a CLI provider).
    # Observation consolidation issues an LLM call per retained memory in the
    # background — on a CLI provider, the user's subscription.
    pipeline = build_pipeline(config, enable_multi_query_expansion=False, enable_observation_consolidation=False)
    brain = Astrocyte(config)
    brain.set_pipeline(pipeline)
    return pipeline, brain


def _bank(args: Namespace) -> str:
    return args.bank or project_bank(str(Path(args.project or os.getcwd()).expanduser().resolve()))


def _when(dt: datetime | None) -> str:
    return dt.date().isoformat() if dt else "          "


def _line(text: str) -> str:
    from .agentd import render_memory

    return render_memory(text, None).removeprefix("- ")


def _source(metadata: Any) -> str:
    return str((metadata or {}).get("source") or "")


async def _all_items(store: Any, bank: str) -> list[Any]:
    items, offset = [], 0
    while True:
        page = await store.list_vectors(bank, offset=offset, limit=500)
        items += page
        if len(page) < 500:
            return items
        offset += 500


# ── commands ─────────────────────────────────────────────────────────────


async def _list(args: Namespace, pipeline: Any, _brain: Any) -> int:
    store, bank = pipeline.vector_store, _bank(args)
    recent = getattr(store, "list_recent_vectors", None)
    items = await recent(bank, limit=args.limit) if recent else (await store.list_vectors(bank, limit=args.limit))
    if args.json:
        print(json.dumps([{"id": i.id, "text": i.text, "source": _source(i.metadata),
                           "when": (i.occurred_at or i.retained_at).isoformat() if (i.occurred_at or i.retained_at)
                           else None} for i in items], indent=2))
        return 0
    if not items:
        print(f"No memories in {bank}.")
        return 0
    count = f"{len(items)} memor{'y' if len(items) == 1 else 'ies'}"
    print(f"{bank} — {len(items)} most recent" if len(items) == args.limit else f"{bank} — {count}")
    for i in items:
        print(f"  {i.id[:8]}  {_when(i.occurred_at or i.retained_at)}  {_source(i.metadata):<11}  {_line(i.text)}")
    print("\nRemove one with: astrocyte memory forget <id>")
    return 0


async def _search(args: Namespace, _pipeline: Any, brain: Any) -> int:
    bank = _bank(args)
    result = await brain.recall(args.query, bank_id=bank, max_results=args.limit)
    hits = [h for h in result.hits if h.text]
    if args.json:
        print(json.dumps([{"id": h.memory_id, "score": round(h.score, 4), "text": h.text} for h in hits], indent=2))
        return 0
    if not hits:
        print(f"Nothing in {bank} matches.")
        return 0
    for h in hits:
        print(f"  {(h.memory_id or '')[:8]}  {_when(h.occurred_at or h.retained_at)}  {_line(h.text)}")
    return 0


async def _forget(args: Namespace, pipeline: Any, brain: Any) -> int:
    bank = _bank(args)
    items = await _all_items(pipeline.vector_store, bank)
    if args.all:
        if not items:
            print(f"No memories in {bank}.")
            return 0
        if not args.yes:
            print(f"This permanently removes all {len(items)} memories in {bank}.\n"
                  "Re-run with --yes to confirm.", file=sys.stderr)
            return 1
        result = await brain.forget(bank, scope="all")
        print(f"Removed {result.deleted_count} memories from {bank}.{await _erase(pipeline, bank, None)}")
        return 0
    if not args.ids:
        print("Name the memories to remove (ids from `astrocyte memory`), or use --all.", file=sys.stderr)
        return 2
    chosen: list[str] = []
    for prefix in args.ids:
        if len(prefix) < MIN_PREFIX:
            print(f"  ✗ {prefix}: give at least {MIN_PREFIX} characters of the id", file=sys.stderr)
            return 2
        matches = [i.id for i in items if i.id.startswith(prefix)]
        if len(matches) != 1:
            why = "no memory in this project has that id" if not matches else f"matches {len(matches)} memories"
            print(f"  ✗ {prefix}: {why}", file=sys.stderr)
            return 1
        chosen.append(matches[0])
    result = await brain.forget(bank, memory_ids=chosen)
    noun = "memory" if result.deleted_count == 1 else "memories"
    print(f"Removed {result.deleted_count} {noun} from {bank}.{await _erase(pipeline, bank, chosen)}")
    return 0


async def _erase(pipeline: Any, bank: str, ids: list[str] | None) -> str:
    purge = getattr(pipeline.vector_store, "purge", None)
    if purge is None:
        return f"\n{type(pipeline.vector_store).__name__} keeps forgotten memories for history; they no longer recall."
    await purge(bank, ids)
    return " Erased from disk."


async def _banks(args: Namespace, pipeline: Any, _brain: Any) -> int:
    lister = getattr(pipeline.vector_store, "list_banks", None)
    if lister is None:
        print(f"{type(pipeline.vector_store).__name__} can't enumerate banks; name one with --bank.",
              file=sys.stderr)
        return 2
    banks = await lister()
    here = _bank(Namespace(bank=None, project=args.project))
    if args.json:
        print(json.dumps([{"bank": b, "memories": n, "newest": t.isoformat() if t else None} for b, n, t in banks],
                         indent=2))
        return 0
    if not banks:
        print("No memories yet.")
        return 0
    for b, n, t in banks:
        print(f"  {'→' if b == here else ' '} {b:<48} {n:>6}  newest {_when(t)}")
    return 0


_COMMANDS = {"list": _list, "search": _search, "forget": _forget, "banks": _banks}


def run(args: Namespace) -> int:
    cfg = Path(args.config).expanduser() if args.config else config_path()
    if not cfg.is_file():
        print(f"No Astrocyte config at {cfg}. Run: astrocyte setup", file=sys.stderr)
        return 2
    pipeline, brain = open_local(cfg)

    async def go() -> int:
        try:
            return await _COMMANDS[args.memory_command or "list"](args, pipeline, brain)
        finally:
            close = getattr(pipeline.vector_store, "close", None)
            if close is not None:
                await close()

    return asyncio.run(go())


def register(sub) -> None:
    memory = sub.add_parser(
        "memory",
        help="See, search and remove what your agents remember (default: this project's recent memories)",
        description="Every command works on the memory bank of the current project — the one automatic "
        "memory and the MCP server use — unless --bank or --project says otherwise.",
    )

    def scope(p, *, limit: int | None = None) -> None:
        p.add_argument("--project", help="project directory (default: the current directory)")
        p.add_argument("--bank", help="a bank id instead of a project (see: astrocyte memory banks)")
        p.add_argument("--config", help="config path (default: ~/.config/astrocyte/astrocyte.yaml)")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        if limit:
            p.add_argument("-n", "--limit", type=int, default=limit, help=f"how many to show (default {limit})")

    scope(memory, limit=LIST_DEFAULT)
    memory.set_defaults(func=run, memory_command=None)
    cmds = memory.add_subparsers(dest="memory_command")
    scope(cmds.add_parser("list", help="most recent memories"), limit=LIST_DEFAULT)
    search = cmds.add_parser("search", help="memories matching a query, as an agent prompt would see them")
    search.add_argument("query")
    scope(search, limit=SEARCH_DEFAULT)
    forget = cmds.add_parser("forget", help="remove memories by id (or prefix), or all of this project's")
    forget.add_argument("ids", nargs="*", help="ids or unique prefixes, from `astrocyte memory`")
    forget.add_argument("--all", action="store_true", help="every memory in the bank")
    forget.add_argument("--yes", action="store_true", help="confirm --all")
    scope(forget)
    scope(cmds.add_parser("banks", help="every bank with memories, this project's marked →"))
