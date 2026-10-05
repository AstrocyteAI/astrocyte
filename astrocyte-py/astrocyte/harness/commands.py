"""``astrocyte setup`` / ``uninstall`` / ``doctor`` command implementations."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

from .choices import load_choices, save_choices
from .doctor import Check, apply_fixes, run_checks
from .hosts import ALL_HOSTS, SUPPORTED_HOSTS, HookHost, Host, Outcome, host_by_key, hosts
from .localconfig import SetupError, choose_providers, render_config, write_config
from .paths import config_path, database_path
from .server import HookPathError, handshake, hook_prefix, locate_mcp_server, server_spec

HOST_KEYS = tuple(cls.key for cls in ALL_HOSTS)  # v0.16.0's value: part of the public API
SUPPORTED_HOST_KEYS = tuple(cls.key for cls in SUPPORTED_HOSTS)

_MARK = {"ok": "✓", "warn": "!", "fail": "✗", "info": "·"}
_OUTCOME_MARK = {
    "installed": "✓", "updated": "✓", "unchanged": "✓", "removed": "✓", "absent": "·",
    "planned": "→", "skipped": "·", "failed": "✗",
}


def _selected_hosts(args: Namespace) -> tuple[list[Host], bool]:
    """Hosts named by flags, else every detected one. Returns (hosts, explicit)."""
    named = [k for k in SUPPORTED_HOST_KEYS if getattr(args, k, False)]
    if named:
        return [host_by_key(k) for k in named], True
    return [h for h in hosts() if h.detected()], False


def _print_outcome(o: Outcome) -> None:
    print(f"  {_OUTCOME_MARK[o.status]} {o.host:<12} {o.status:<9} {o.detail}")


def cmd_setup(args: Namespace) -> int:
    cfg_path = Path(args.config).expanduser() if args.config else config_path()
    dry = args.dry_run

    print("Astrocyte setup\n")
    # 1. Config — the user's file once written; never overwritten.
    if cfg_path.is_file():
        print(f"  ✓ config       keeping existing {cfg_path}")
    else:
        try:
            choice = choose_providers()
        except SetupError as e:
            print(f"  ✗ config       {e}", file=sys.stderr)
            return 1
        if dry:
            print(f"  → config       would write {cfg_path}")
        else:
            write_config(cfg_path, render_config(choice, database_path()))
            print(f"  ✓ config       wrote {cfg_path}")
        print(f"                 completions: {choice.llm_why}")
        if choice.embedding_why:
            print(f"                 embeddings:  {choice.embedding_why}")
        print(f"                 memories:    {database_path()}")

    # 2. The server binary from *this* installation.
    found = locate_mcp_server()
    if found is None:
        print("  ✗ server       astrocyte-mcp not found. Install with: uv tool install 'astrocyte[local]'",
              file=sys.stderr)
        return 1
    if found.ephemeral:
        print(f"  ! server       {found.command} is in a cache that may be pruned;\n"
              "                 for a durable install run: uv tool install 'astrocyte[local]'")
    spec = server_spec(found.command, cfg_path)

    # 3. Prove the server starts before wiring anything to it.
    if not dry and not args.no_verify:
        result = handshake(spec)
        if not result.ok:
            print(f"  ✗ server       failed to start: {result.detail}\n"
                  "                 nothing was wired. Fix the above, then re-run.", file=sys.stderr)
            return 1
        print(f"  ✓ server       starts and answers ({result.detail}, {result.seconds:.1f}s)")
        warmed = _warm_embeddings(cfg_path)
        if warmed:
            print(f"  ✓ model        {warmed}")

    # Memories hold conversations: a store other accounts can read (created
    # before stores were made private) is tightened here.
    if not dry:
        from .doctor import _make_store_private

        for line in _make_store_private(cfg_path):
            print(f"  ✓ store        {line}")

    # 4. Harnesses. Naming one records the choice made for it; a plain run
    # leaves alone what the user switched off before.
    targets, explicit = _selected_hosts(args)
    if not targets:
        print("\n  No agent harnesses detected. Supported: " + ", ".join(c.label for c in SUPPORTED_HOSTS)
              + ".\n  Install one, or name it explicitly, e.g. astrocyte setup --claude")
        return 1
    choices = load_choices(cfg_path)
    if explicit:
        for h in targets:
            choices.off.discard(h.key)
            (choices.hooks_off.add if args.no_hooks else choices.hooks_off.discard)(h.key)
    for h in targets:  # opt-in file recall, where the harness has a file hook
        if getattr(h, "FILE_HOOK", None) and getattr(args, "file_recall", False):
            choices.file_recall.add(h.key)
        elif getattr(args, "no_file_recall", False):
            choices.file_recall.discard(h.key)
    else:
        print("\n  Detected: " + ", ".join(h.label for h in targets))
        if args.no_hooks:
            choices.hooks_off |= {h.key for h in targets if isinstance(h, HookHost)}
    switched_off = [h for h in targets if h.key in choices.off]
    targets = [h for h in targets if h.key not in choices.off]
    print()
    outcomes = [Outcome(h.label, "skipped", f"switched off; astrocyte setup --{h.key} turns it back on")
                for h in switched_off]
    outcomes += [h.install(spec, dry_run=dry) for h in targets]

    # 5. Automatic memory (lifecycle hooks, where the harness has them).
    hook_hosts = [h for h in targets if isinstance(h, HookHost)]
    auto_memory = [h for h in hook_hosts if h.key not in choices.hooks_off]
    try:
        prefix, prefix_error = hook_prefix(found.command), ""
    except HookPathError as e:  # Windows: a path no shell-neutral command can name
        prefix, prefix_error = None, str(e)
    for h in hook_hosts:
        if h in auto_memory:
            extra = {"file_recall": h.key in choices.file_recall} if h.FILE_HOOK else {}
            outcomes.append(h.install_hooks(prefix, dry_run=dry, **extra) if prefix
                            else Outcome(f"{h.label} hooks", "failed", prefix_error))
            continue
        # Off by choice (--no-hooks, now or before), including a previous install.
        outcome = h.uninstall_hooks(dry_run=dry)
        if outcome.status != "absent":
            outcomes.append(outcome)
        elif not args.no_hooks:
            outcomes.append(Outcome(f"{h.label} hooks", "skipped",
                                    f"automatic memory off; astrocyte setup --{h.key} turns it on"))
    for o in outcomes:
        _print_outcome(o)
    if not dry:
        save_choices(cfg_path, choices)
    if not targets:
        print("\nEvery detected agent is switched off; nothing was wired. "
              f"Name one to turn it back on, e.g. astrocyte setup --{switched_off[0].key}")
        return 0

    failed = [o for o in outcomes if o.status == "failed"]
    if dry:
        print("\nDry run — nothing was changed.")
    elif failed:
        print(f"\n{len(failed)} harness(es) could not be wired; see above. `astrocyte doctor` re-checks.")
    else:
        fresh = {o.host for o in outcomes if o.status in ("installed", "updated")}
        notes = [h.next_step for h in targets if h.label in fresh and h.next_step]
        notes += [h.hooks_hint() for h in hook_hosts if f"{h.label} hooks" in fresh and h.hooks_hint()]
        if notes:
            print("\nOne more step in some agents:")
            for note in notes:
                print(f"  • {note}")
        if auto_memory:
            capture = [h.label for h in auto_memory if h.captures_turns]
            recall_only = [h.label for h in auto_memory if not h.captures_turns]
            print("\nAutomatic memory is on in interactive sessions, including ones already open.")
            if capture:
                print(f"  {_join(capture)}: each finished turn is saved to that project's local memory,\n"
                      "  and relevant memories are added to new prompts.")
            if recall_only:
                print(f"  {_join(recall_only)}: relevant memories (saved by your other agents) are added to\n"
                      "  new prompts; its own turns are not saved yet.")
            file_recall = [h.label for h in auto_memory if h.FILE_HOOK and h.key in choices.file_recall]
            if file_recall:
                print(f"  {_join(file_recall)}: after it reads or edits a file, memories of earlier turns that\n"
                      "  touched that file are added too (file recall; off with --no-file-recall).")
            print("Headless runs (`claude -p`, `codex exec`, `agy -p`, `copilot -p`) are left alone.\n"
                  "Pause with ASTROCYTE_HOOKS=off, or remove with: astrocyte setup --no-hooks")
        seed = _seed_files(Path.cwd())
        if seed:
            print("\nGive this project's memory a head start from its agent instructions:\n"
                  f"  astrocyte memory import {' '.join(seed)}")
        print("\nDone. Start a new session in your agent to load the Astrocyte memory tools.\n"
              "Verify any time with: astrocyte doctor")
    return 1 if failed else 0


# Instruction files coding agents already read: importing them means memory
# starts with the project's conventions instead of empty.
SEED_FILES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".github/copilot-instructions.md", ".cursorrules")


def _seed_files(cwd: Path) -> list[str]:
    from .project import project_root

    root = project_root(str(cwd))
    if not (root / ".git").exists():
        return []  # not standing in a project
    return [name for name in SEED_FILES if (root / name).is_file()]


def _warm_embeddings(cfg_path: Path) -> str:
    """Fetch and load a local embedding model now (bge-small, ~130 MB on first
    run), so the first session doesn't wait for the download. Best effort:
    the server already started; a failure here only means a slower first use."""
    import asyncio
    import time

    from astrocyte.config import load_config
    from astrocyte.wiring import resolve_llm_provider

    try:
        config = load_config(str(cfg_path))
        if config.embedding_provider != "local_embeddings":
            return ""
        started = time.perf_counter()
        print("  … model        loading the local embedding model (downloaded once, ~130 MB)", flush=True)
        asyncio.run(resolve_llm_provider(config).embed(["warm"]))
        return f"local embedding model ready ({time.perf_counter() - started:.1f}s)"
    except Exception as e:  # noqa: BLE001 — never fail setup over a warm-up
        return f"could not load the embedding model yet ({type(e).__name__}); it will load on first use"


def cmd_uninstall(args: Namespace) -> int:
    targets, explicit = _selected_hosts(args)
    print("Removing Astrocyte from agent harnesses\n")
    outcomes = [h.uninstall(dry_run=args.dry_run) for h in targets]
    outcomes += [h.uninstall_hooks(dry_run=args.dry_run) for h in targets if isinstance(h, HookHost)]
    for o in outcomes:
        _print_outcome(o)
    cfg_path = config_path()
    if explicit:
        # Removing one agent is a standing choice; removing them all (no
        # flags) is a teardown, which a later `astrocyte setup` reverses.
        if not args.dry_run:
            choices = load_choices(cfg_path)
            choices.off |= {h.key for h in targets}
            save_choices(cfg_path, choices)
        print(f"\nastrocyte setup will leave {_join([h.label for h in targets])} switched off;\n"
              f"turn it back on with: astrocyte setup {' '.join('--' + h.key for h in targets)}")
    print(f"\nYour config ({cfg_path}) and memories ({database_path()}) were left in place;\n"
          "delete them yourself if you no longer want them.")
    return 1 if any(o.status == "failed" for o in outcomes) else 0


def _render(checks: list[Check]) -> None:
    width = max(len(c.area) for c in checks)
    for c in checks:
        print(f"  {_MARK[c.level]} {c.area:<{width}}  {c.summary}")
        if c.fix and c.level in ("fail", "warn"):
            print(f"    {' ' * width}  fix: {c.fix}")


def cmd_doctor(args: Namespace) -> int:
    cfg_path = Path(args.config).expanduser() if args.config else config_path()
    checks = run_checks(cfg_path, model_probes=not args.skip_models)
    repaired: list[str] = []
    if args.fix and any(c.fixable for c in checks):
        repaired = apply_fixes(cfg_path, checks)
        checks = run_checks(cfg_path, model_probes=not args.skip_models)

    failures = [c for c in checks if c.level == "fail"]
    if args.json:
        print(json.dumps({"ok": not failures, "checks": [c.as_dict() for c in checks], "repaired": repaired},
                         indent=2))
        return 1 if failures else 0

    print("Astrocyte doctor\n")
    if repaired:
        print("Repaired:")
        for line in repaired:
            print(f"  ✓ {line}")
        print()
    _render(checks)
    if failures:
        fixable = any(c.fixable for c in failures)
        hint = " Run `astrocyte doctor --fix` to repair what it can." if fixable and not args.fix else ""
        print(f"\n{len(failures)} problem(s).{hint}")
        return 1
    print("\nAll good.")
    return 0


def register(sub) -> None:
    """Add setup / uninstall / doctor to the ``astrocyte`` CLI."""

    def host_flags(p) -> None:
        for cls in SUPPORTED_HOSTS:
            p.add_argument(f"--{cls.key}", action="store_true", help=f"only {cls.label}")
        p.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")

    setup = sub.add_parser(
        "setup",
        help="Create a local memory store and wire it into your coding agents",
        description="Writes ~/.config/astrocyte/astrocyte.yaml (if absent), verifies the MCP server starts, "
        "then registers it with every detected agent harness (or only those named).",
    )
    host_flags(setup)
    setup.add_argument("--config", help="config path (default: ~/.config/astrocyte/astrocyte.yaml)")
    setup.add_argument("--no-verify", action="store_true", help="skip the server start-up check")
    setup.add_argument("--file-recall", action="store_true",
                       help="also recall memories of earlier turns when the agent reads or edits a file "
                       "(Claude Code; opt-in, remembered)")
    setup.add_argument("--no-file-recall", action="store_true", help="turn file recall off again")
    setup.add_argument("--no-hooks", action="store_true",
                       help="don't enable automatic memory (Claude Code / Codex capture + recall hooks); "
                       "removes them if present")
    setup.set_defaults(func=cmd_setup)

    hook = sub.add_parser("hook", help="(called by agent hooks) automatic memory for one lifecycle event")
    hook.add_argument("event", choices=["session-start", "prompt", "stop", "file", "edit"])
    hook.add_argument("--host", choices=["claude", "codex", "antigravity", "copilot"], default="claude",
                      help="the agent firing the hook")
    hook.set_defaults(func=lambda a: _run_hook(a.event, a.host))

    agentd = sub.add_parser("agentd", help="(started on demand) keep memory warm for agent hooks")
    agentd.add_argument("--config", help="config path (default: ~/.config/astrocyte/astrocyte.yaml)")
    agentd.set_defaults(func=lambda a: _run_agentd(a.config))

    uninstall = sub.add_parser("uninstall", help="Remove Astrocyte from agent harnesses (keeps your memories)")
    host_flags(uninstall)
    uninstall.set_defaults(func=cmd_uninstall)

    doctor = sub.add_parser("doctor", help="Check the local install end to end; --fix repairs it")
    doctor.add_argument("--fix", action="store_true", help="repair a missing config and broken harness entries")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.add_argument("--skip-models", action="store_true", help="skip the embedding/completion probes")
    doctor.add_argument("--config", help="config path (default: ~/.config/astrocyte/astrocyte.yaml)")
    doctor.set_defaults(func=cmd_doctor)

    from .memories import register as register_memory

    register_memory(sub)


def _run_hook(event: str, host: str) -> int:
    from .hooks import main

    return main(event, host)


def _run_agentd(config: str | None) -> int:
    from .agentd import run

    return run(Path(config).expanduser() if config else config_path())


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
