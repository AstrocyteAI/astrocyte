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
import hashlib
import json
import os
import re
import sys
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import config_path
from .project import project_bank, project_root

LIST_DEFAULT = 20
SEARCH_DEFAULT = 8
MIN_PREFIX = 4  # shortest id prefix `forget` accepts


def open_local(cfg_path: Path, *, noisy_bank_detection: bool = True) -> tuple[Any, Any]:
    """``(pipeline, brain)`` for the local config, wired the way the agent
    daemon needs it: no LLM call on recall, none in the background.

    ``noisy_bank_detection=False`` is for the user's own commands: noisy-bank
    detection guards against an *agent* writing junk or looping, and a bulk
    ``astrocyte memory import`` of short doc sections is neither — flagging
    it would only print a warning at the person who asked for the import.
    """
    from astrocyte import Astrocyte
    from astrocyte.config import load_config
    from astrocyte.wiring import build_pipeline

    config = load_config(str(cfg_path))
    if not noisy_bank_detection:
        config.signal_quality.noisy_bank.enabled = False
        for bank in (config.banks or {}).values():
            if bank.signal_quality is not None:
                bank.signal_quality.noisy_bank.enabled = False
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
        print(
            json.dumps(
                [
                    {
                        "id": i.id,
                        "text": i.text,
                        "source": _source(i.metadata),
                        "when": (i.occurred_at or i.retained_at).isoformat()
                        if (i.occurred_at or i.retained_at)
                        else None,
                    }
                    for i in items
                ],
                indent=2,
            )
        )
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
            print(
                f"This permanently removes all {len(items)} memories in {bank}.\nRe-run with --yes to confirm.",
                file=sys.stderr,
            )
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
        print(f"{type(pipeline.vector_store).__name__} can't enumerate banks; name one with --bank.", file=sys.stderr)
        return 2
    banks = await lister()
    here = _bank(Namespace(bank=None, project=args.project))
    if args.json:
        print(
            json.dumps(
                [{"bank": b, "memories": n, "newest": t.isoformat() if t else None} for b, n, t in banks], indent=2
            )
        )
        return 0
    if not banks:
        print("No memories yet.")
        return 0
    for b, n, t in banks:
        print(f"  {'→' if b == here else ' '} {b:<48} {n:>6}  newest {_when(t)}")
    return 0


# ── import / export ──────────────────────────────────────────────────────

IMPORT_SUFFIXES = (".md", ".mdx", ".markdown", ".txt")
IMPORT_SKIP_DIRS = frozenset({"node_modules", "venv", ".venv", "dist", "build", "__pycache__", "site-packages"})
IMPORT_MAX_FILE_BYTES = 512 * 1024
SECTION_MAX_CHARS = 3_000
_HEADING = re.compile(r"^#{1,3}\s+\S")
_FENCE = re.compile(r"^\s*(```|~~~)")


def import_files(paths: list[str]) -> list[Path]:
    """Files named, plus the Markdown/text files under directories named —
    skipping hidden, vendored and build directories and very large files."""
    found: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file():
            found.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                rel = f.relative_to(p).parts
                if any(part.startswith(".") or part in IMPORT_SKIP_DIRS for part in rel[:-1]):
                    continue
                if f.is_file() and f.suffix.lower() in IMPORT_SUFFIXES and f.stat().st_size <= IMPORT_MAX_FILE_BYTES:
                    found.append(f)
        else:
            raise FileNotFoundError(raw)
    return list(dict.fromkeys(found))


def sections(text: str) -> list[str]:
    """Split Markdown at #–### headings (not inside code fences), each section
    keeping its heading; long sections are split again at blank lines. One
    focused memory per topic recalls better than one memory per file."""
    parts: list[list[str]] = [[]]
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        if not fenced and _HEADING.match(line) and any(x.strip() for x in parts[-1]):
            parts.append([])
        parts[-1].append(line)
    out: list[str] = []
    for part in parts:
        body = "\n".join(part).strip()
        while len(body) > SECTION_MAX_CHARS:
            cut = body.rfind("\n\n", 0, SECTION_MAX_CHARS)
            cut = cut if cut > SECTION_MAX_CHARS // 3 else SECTION_MAX_CHARS
            out.append(body[:cut].strip())
            body = body[cut:].strip()
        if body:
            out.append(body)
    return out


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _label(path: Path, args: Namespace) -> str:
    """How an imported file is identified: relative to the project root, so
    the same file in another clone of the repo is recognised."""
    root = project_root(str(Path(args.project or os.getcwd()).expanduser().resolve()))
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path.resolve())


async def _import(args: Namespace, pipeline: Any, brain: Any) -> int:
    """Seed a project's memory from documents, as a sync: re-importing a file
    adds its new sections and removes the ones no longer in it."""
    bank = _bank(args)
    if len(args.paths) == 1 and args.paths[0].endswith(".ama.jsonl"):
        return await _import_archive(args, brain, bank)
    try:
        files = import_files(args.paths)
    except FileNotFoundError as e:
        print(f"  ✗ no such file or directory: {e}", file=sys.stderr)
        return 2
    if not files:
        print("Nothing to import (Markdown and text files: " + ", ".join(IMPORT_SUFFIXES) + ").")
        return 0
    held: dict[str, dict[str, list[str]]] = {}  # import path → section hash → memory ids
    for item in await _all_items(pipeline.vector_store, bank):
        md = item.metadata or {}
        if md.get("import_path") and md.get("import_hash"):
            held.setdefault(str(md["import_path"]), {}).setdefault(str(md["import_hash"]), []).append(item.id)
    added = kept = removed = duplicates = 0
    for f in files:
        label = _label(f, args)
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"  ✗ {label}: {e}", file=sys.stderr)
            continue
        wanted = {_digest(s): s for s in sections(text)}
        have = held.get(label, {})
        new = [(h, s) for h, s in wanted.items() if h not in have]
        stale = [mid for h, ids in have.items() if h not in wanted for mid in ids]
        kept += len(wanted) - len(new)
        if args.dry_run:
            print(f"  {label}: {len(new)} to add, {len(stale)} to remove, {len(wanted) - len(new)} unchanged")
            added, removed = added + len(new), removed + len(stale)
            continue
        when = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        stored = dup = 0
        for digest, section in new:
            # The pipeline's dedup checks the store too, so a section already
            # said elsewhere — in this run or an earlier one — is skipped.
            result = await brain.retain(
                section,
                bank_id=bank,
                occurred_at=when,
                source="import",
                metadata={"source": "import", "import_path": label, "import_hash": digest},
            )
            if result.stored:
                stored += 1
            elif getattr(result, "deduplicated", False):
                dup += 1
        if stale:
            removed += (await brain.forget(bank, memory_ids=stale)).deleted_count
            await _erase(pipeline, bank, stale)
        added, duplicates = added + stored, duplicates + dup
        dup_note = f", {dup} duplicates skipped" if dup else ""
        print(f"  ✓ {label}: {stored} added, {len(stale)} removed, {len(wanted) - len(new)} unchanged{dup_note}")
    verb = "Would add" if args.dry_run else "Added"
    dup_note = f", skipped {duplicates} near-duplicates of existing memories" if duplicates else ""
    print(f"\n{verb} {added}, removed {removed}, kept {kept}{dup_note} in {bank}.")
    return 0


async def _import_archive(args: Namespace, brain: Any, bank: str) -> int:
    path = Path(args.paths[0]).expanduser().resolve()
    if args.dry_run:
        print(f"  would import {path} into {bank}")
        return 0
    result = await brain.import_bank(bank, str(path), allowed_roots=[str(path.parent)])
    print(f"Imported {result.imported} memories into {bank} ({result.skipped} skipped).")
    return 0 if not getattr(result, "errors", None) else 1


async def _export(args: Namespace, pipeline: Any, _brain: Any) -> int:
    """Write the bank as an Astrocyte Memory Archive (AMA JSONL), read straight
    from the store so nothing is missed. Owner-only: it holds conversations."""
    from astrocyte.portability import AMA_VERSION

    bank = _bank(args)
    items = await _all_items(pipeline.vector_store, bank)
    dest = Path(args.file).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "_ama_version": AMA_VERSION,
                    "bank_id": bank,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "provider": "astrocyte-cli",
                    "memory_count": len(items),
                }
            )
            + "\n"
        )
        for item in items:
            record: dict[str, Any] = {"id": item.id, "text": item.text, "bank_id": bank}
            if item.fact_type:
                record["fact_type"] = item.fact_type
            if item.tags:
                record["tags"] = item.tags
            if item.metadata:
                record["metadata"] = item.metadata
                if item.metadata.get("source"):
                    record["source"] = item.metadata["source"]
            if item.occurred_at or item.retained_at:
                record["occurred_at"] = (item.occurred_at or item.retained_at).isoformat()
            fh.write(json.dumps(record, default=str) + "\n")
    print(f"Exported {len(items)} memories from {bank} to {dest}.")
    return 0


_COMMANDS = {"list": _list, "search": _search, "forget": _forget, "banks": _banks, "import": _import, "export": _export}


def run(args: Namespace) -> int:
    cfg = Path(args.config).expanduser() if args.config else config_path()
    if not cfg.is_file():
        print(f"No Astrocyte config at {cfg}. Run: astrocyte setup", file=sys.stderr)
        return 2
    pipeline, brain = open_local(cfg, noisy_bank_detection=False)

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
    imp = cmds.add_parser(
        "import",
        help="seed memory from Markdown/text files or directories (or an .ama.jsonl archive)",
        description="Each file is split at its headings into one memory per section. Re-importing a file "
        "syncs it: new sections are added, sections no longer in the file are removed. Credentials are "
        "redacted as with any memory. Typical first step: astrocyte memory import CLAUDE.md AGENTS.md docs/",
    )
    imp.add_argument("paths", nargs="+", help="files, directories, or one .ama.jsonl archive")
    imp.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    scope(imp)
    exp = cmds.add_parser("export", help="write the bank to an .ama.jsonl archive (portable; re-import elsewhere)")
    exp.add_argument("file", help="destination, e.g. project.ama.jsonl")
    scope(exp)
