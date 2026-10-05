"""``astrocyte doctor``: verify a local install end to end, and repair it.

Static checks (does the config exist, is the path registered) are necessary
but proved insufficient: the published Claude Code plugin passed all of them
and still crashed on first use with a missing dependency. So doctor also
*runs* things — the store, the models, and the MCP server exactly as each
harness will launch it.

``--fix`` repairs what setup owns: a missing config, and harness entries that
are stale or broken. It never adds Astrocyte to a harness the user did not
wire — "not wired" is reported, not "fixed".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from .choices import Choices, load_choices
from .hosts import HookHost, Host, HostConfigError, hosts
from .server import handshake, locate_mcp_server, server_spec

Level = Literal["ok", "warn", "fail", "info"]


@dataclass
class Check:
    area: str
    level: Level
    summary: str
    fix: str = ""
    fixable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _check_install(found) -> list[Check]:
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("astrocyte")
    except PackageNotFoundError:
        installed = "(not installed as a package)"
    out = [Check("install", "ok", f"astrocyte {installed}")]
    if found is None:
        out.append(Check("install", "fail", "astrocyte-mcp not found in this installation",
                         fix="reinstall with: uv tool install 'astrocyte[local]'"))
    elif found.ephemeral:
        out.append(Check("install", "warn", f"astrocyte-mcp runs from a cache that may be pruned ({found.command})",
                         fix="install persistently: uv tool install 'astrocyte[local]', then astrocyte setup"))
    else:
        out.append(Check("install", "ok", f"server runs as {found.command} -I -m astrocyte.mcp"))
    return out


def _load(config_path: Path) -> tuple[Any | None, list[Check]]:
    from astrocyte.config import load_config

    if not config_path.is_file():
        return None, [Check("config", "fail", f"no config at {config_path}",
                            fix="astrocyte setup (or astrocyte doctor --fix)", fixable=True)]
    try:
        config = load_config(str(config_path))
    except Exception as e:  # noqa: BLE001 — report any parse failure
        return None, [Check("config", "fail", f"{config_path} does not load: {e}", fix="correct the YAML")]
    return config, [Check("config", "ok", str(config_path))]


async def _check_store(config) -> list[Check]:
    from astrocyte.wiring import resolve_store

    try:
        store = resolve_store(config, "vector_store")
    except Exception as e:  # noqa: BLE001
        return [Check("store", "fail", f"vector_store cannot be built: {e}")]
    if store is None:
        return [Check("store", "fail", "no vector_store configured", fix="set vector_store (e.g. sqlite)")]
    status = await store.health()
    out = [Check("store", "ok" if status.healthy else "fail", status.message or type(store).__name__)]
    path = getattr(store, "path", None)
    if isinstance(path, str) and path:
        from .privacy import exposed

        if open_to_others := exposed(Path(path)):
            out.append(Check(
                "store", "warn",
                "other accounts on this machine can read your memories: "
                + ", ".join(p.name or str(p) for p in open_to_others),
                fix="astrocyte doctor --fix", fixable=True,
            ))
    return out


async def _check_models(config) -> list[Check]:
    from astrocyte.types import Message
    from astrocyte.wiring import resolve_llm_provider

    try:
        llm = resolve_llm_provider(config)
    except Exception as e:  # noqa: BLE001
        return [Check("models", "fail", f"provider cannot be built: {e}")]
    if type(llm).__name__ == "MockLLMProvider":
        return [Check("models", "warn", "mock provider configured: memories will not be meaningfully recalled",
                      fix="run astrocyte setup to pick a real provider")]
    out: list[Check] = []
    started = time.perf_counter()
    try:
        vecs = await llm.embed(["astrocyte doctor probe"])
        out.append(Check("models", "ok", f"embeddings: {len(vecs[0])}-dim in {time.perf_counter() - started:.1f}s"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("models", "fail", f"embedding failed: {type(e).__name__}: {e}"))
    started = time.perf_counter()
    try:
        reply = await llm.complete([Message(role="user", content="Reply with exactly: OK")], max_tokens=8)
        out.append(Check("models", "ok", f"completions: replied in {time.perf_counter() - started:.1f}s"
                         f" ({(reply.text or '').strip()[:20]!r})"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("models", "fail", f"completion failed: {type(e).__name__}: {e}"))
    return out


def _check_host(host: Host, expected, choices: Choices | None = None) -> Check:
    try:
        reg = host.registration()
    except HostConfigError as e:
        return Check(host.label, "fail", str(e))
    if reg is None:
        if choices is not None and host.key in choices.off:
            return Check(host.label, "info", f"switched off; astrocyte setup --{host.key} turns it back on")
        return Check(host.label, "info", "installed, Astrocyte not wired",
                     fix=f"astrocyte setup --{host.key}")
    if not Path(reg.command).exists():
        return Check(host.label, "fail", f"points at a missing server: {reg.command}",
                     fix="astrocyte doctor --fix", fixable=expected is not None)
    if expected is not None and not reg.matches(expected):
        return Check(host.label, "warn", f"registered with a different command or config: {reg.command} "
                     f"{' '.join(reg.args)}", fix="astrocyte doctor --fix", fixable=True)
    return Check(host.label, "ok", f"wired ({host.config_file()})")


def _probe_shells() -> list[tuple[str, list[str]]]:
    """The shells an agent may run a hook command through on this machine."""
    if os.name != "nt":
        return [("sh", ["sh", "-c"])]
    shells = [("cmd", ["cmd", "/d", "/s", "/c"])]
    if ps := shutil.which("pwsh") or shutil.which("powershell"):
        shells.append(("PowerShell", [ps, "-NoProfile", "-NonInteractive", "-Command"]))
    # Git Bash, the bash Claude Code uses on Windows; never WSL's System32\bash.exe.
    if bash := os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or _git_bash():
        shells.append(("Git Bash", [bash, "-c"]))
    return shells


def _git_bash() -> str | None:
    """Git for Windows' bash, found the way Claude Code looks for it: the
    standard install locations, then beside the ``git`` on PATH (which may be
    Git\\cmd\\git.exe or Git\\bin\\git.exe)."""
    candidates = [Path(os.environ[var]) / "Git" / "bin" / "bash.exe"
                  for var in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(var)]
    if git := shutil.which("git"):
        here = Path(git).resolve().parent
        candidates += [here / "bash.exe", here.parent / "bin" / "bash.exe", here.parent / "usr" / "bin" / "bash.exe"]
    return next((str(c) for c in candidates if c.is_file()), None)


def _probe(command: str, shell: list[str]) -> str | None:
    """None if ``command`` answers a ping when run through ``shell``; else why not."""
    try:
        proc = subprocess.run([*shell, command], input='{"ping": true}', capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return f"{type(e).__name__}: {e}"
    if proc.returncode == 0 and '"pong"' in proc.stdout:
        return None
    last = (proc.stderr or proc.stdout).strip().splitlines()[-1:]
    return f"exit {proc.returncode}" + (f": {last[0][:160]}" if last else "")


def _check_hooks(host, choices: Choices | None = None) -> Check:
    """One harness's lifecycle hooks (automatic memory)."""
    from .server import HookPathError, hook_prefix, locate_mcp_server

    label = f"{host.label} hooks"
    try:
        commands = host.hook_commands()
    except HostConfigError as e:
        return Check(label, "fail", str(e))
    if not any(commands.values()):
        if choices is not None and host.key in choices.hooks_off:
            return Check(label, "info", f"automatic memory switched off; astrocyte setup --{host.key} turns it on")
        return Check(label, "info", "automatic memory off", fix=f"astrocyte setup --{host.key}")
    missing = [event for event, cmd in commands.items() if not cmd]
    found = locate_mcp_server()
    try:
        prefix = hook_prefix(found.command) if found else None
    except HookPathError as e:
        return Check(label, "fail", str(e))
    file_cmd = host.file_recall_command() if isinstance(host, HookHost) else None
    stale = [event for event, cmd in {**commands, "file recall": file_cmd}.items()
             if cmd and prefix and not cmd.startswith(prefix + " ")]
    if missing or stale:
        detail = ", ".join([f"{e} missing" for e in missing] + [f"{e} points at another install" for e in stale])
        return Check(label, "fail", detail, fix="astrocyte doctor --fix", fixable=prefix is not None)
    # Run it for real: a command the agent's shell can't parse would fail
    # silently in every session (the agent ignores a failing hook).
    command = next(cmd for cmd in commands.values() if cmd)
    broken = [f"{name}: {why}" for name, shell in _probe_shells() if (why := _probe(command, shell))]
    if broken:
        return Check(label, "fail", "hook command does not run under " + "; ".join(broken))
    extra = ", with file recall" if file_cmd else ""
    return Check(label, "ok", f"automatic memory on{extra} ({host.hooks_file()})")


def _check_daemon() -> Check:
    """The per-user daemon every harness's hooks talk to."""
    from . import agentd

    if not agentd.supported():
        return Check("agent daemon", "warn", "no local transport; automatic recall is off on this OS")
    ping = agentd.request("ping", timeout=0.5)
    return Check("agent daemon", "ok" if ping else "info",
                 f"running (pid {ping['pid']})" if ping else "not running (starts with the next session)")


def run_checks(config_path: Path, *, model_probes: bool = True) -> list[Check]:
    found = locate_mcp_server()
    checks = _check_install(found)
    config, config_checks = _load(config_path)
    checks += config_checks
    expected = server_spec(found.command, config_path) if found else None

    if config is not None:
        import asyncio  # here, not at import: every agent hook loads this module

        checks += asyncio.run(_check_store(config))
        if model_probes:
            checks += asyncio.run(_check_models(config))
        if expected is not None:
            result = handshake(expected)
            checks.append(Check(
                "server", "ok" if result.ok else "fail",
                f"MCP handshake: {result.detail} in {result.seconds:.1f}s" if result.ok
                else f"MCP server failed to start: {result.detail}",
            ))

    choices = load_choices(config_path)
    hook_checks: list[Check] = []
    for host in hosts():
        if host.detected():
            check = _check_host(host, expected, choices)
            checks.append(check)
            if isinstance(host, HookHost) and not check.summary.startswith("switched off"):
                hook_checks.append(_check_hooks(host, choices))
    checks += hook_checks
    if any(c.level != "info" for c in hook_checks):
        checks.append(_check_daemon())
    return checks


def _make_store_private(config_path: Path) -> list[str]:
    from astrocyte.config import load_config
    from astrocyte.wiring import resolve_store

    from .privacy import make_private

    try:
        store = resolve_store(load_config(str(config_path)), "vector_store")
    except Exception as e:  # noqa: BLE001
        return [f"could not open the store to fix permissions: {e}"]
    path = getattr(store, "path", None)
    if not isinstance(path, str) or not path:
        return []
    return [f"made private: {p}" for p in make_private(Path(path))]


def apply_fixes(config_path: Path, checks: list[Check]) -> list[str]:
    """Repair what setup owns. Returns a line per action taken."""
    from .localconfig import SetupError, choose_providers, render_config, write_config
    from .paths import database_path

    done: list[str] = []
    if any(c.area == "config" and c.fixable for c in checks):
        try:
            write_config(config_path, render_config(choose_providers(), database_path()))
            done.append(f"wrote {config_path}")
        except SetupError as e:
            done.append(f"could not write config: {e}")
    if any(c.area == "store" and c.fixable for c in checks):
        done += _make_store_private(config_path)
    found = locate_mcp_server()
    if found is None:
        return done
    from .server import HookPathError, hook_prefix

    spec = server_spec(found.command, config_path)
    broken = {c.area for c in checks if c.fixable and c.area not in ("config",)}
    for host in hosts():
        if host.label in broken:
            outcome = host.install(spec)
            done.append(f"{host.label}: {outcome.status} {outcome.detail}".rstrip())
        if f"{host.label} hooks" in broken:
            try:
                outcome = host.install_hooks(hook_prefix(found.command))
            except HookPathError as e:
                done.append(f"{host.label} hooks: not repaired: {e}")
                continue
            done.append(f"{host.label} hooks: {outcome.status} {outcome.detail}".rstrip())
    return done
