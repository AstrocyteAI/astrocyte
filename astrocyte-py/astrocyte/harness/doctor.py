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

import asyncio
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

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
    return [Check("store", "ok" if status.healthy else "fail", status.message or type(store).__name__)]


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


def _check_host(host: Host, expected) -> Check:
    try:
        reg = host.registration()
    except HostConfigError as e:
        return Check(host.label, "fail", str(e))
    if reg is None:
        return Check(host.label, "info", "installed, Astrocyte not wired",
                     fix=f"astrocyte setup --{host.key}")
    if not Path(reg.command).exists():
        return Check(host.label, "fail", f"points at a missing server: {reg.command}",
                     fix="astrocyte doctor --fix", fixable=expected is not None)
    if expected is not None and not reg.matches(expected):
        return Check(host.label, "warn", f"registered with a different command or config: {reg.command} "
                     f"{' '.join(reg.args)}", fix="astrocyte doctor --fix", fixable=True)
    return Check(host.label, "ok", f"wired ({host.config_file()})")


def _check_hooks(host) -> Check:
    """One harness's lifecycle hooks (automatic memory)."""
    from .server import hook_prefix, locate_mcp_server

    label = f"{host.label} hooks"
    try:
        commands = host.hook_commands()
    except HostConfigError as e:
        return Check(label, "fail", str(e))
    if not any(commands.values()):
        return Check(label, "info", "automatic memory off", fix=f"astrocyte setup --{host.key}")
    missing = [event for event, cmd in commands.items() if not cmd]
    found = locate_mcp_server()
    prefix = hook_prefix(found.command) if found else None
    stale = [event for event, cmd in commands.items() if cmd and prefix and not cmd.startswith(prefix + " ")]
    if missing or stale:
        detail = ", ".join([f"{e} missing" for e in missing] + [f"{e} points at another install" for e in stale])
        return Check(label, "fail", detail, fix="astrocyte doctor --fix", fixable=prefix is not None)
    return Check(label, "ok", f"automatic memory on ({host.hooks_file()})")


def _check_daemon() -> Check:
    """The per-user daemon every harness's hooks talk to."""
    from . import agentd

    if not agentd.supported():
        return Check("agent daemon", "warn", "needs Unix sockets; automatic recall is off on this OS")
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

    hook_checks: list[Check] = []
    for host in hosts():
        if host.detected():
            checks.append(_check_host(host, expected))
            if isinstance(host, HookHost):
                hook_checks.append(_check_hooks(host))
    checks += hook_checks
    if any(c.level != "info" for c in hook_checks):
        checks.append(_check_daemon())
    return checks


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
    found = locate_mcp_server()
    if found is None:
        return done
    from .server import hook_prefix

    spec = server_spec(found.command, config_path)
    broken = {c.area for c in checks if c.fixable and c.area not in ("config",)}
    for host in hosts():
        if host.label in broken:
            outcome = host.install(spec)
            done.append(f"{host.label}: {outcome.status} {outcome.detail}".rstrip())
        if f"{host.label} hooks" in broken:
            outcome = host.install_hooks(hook_prefix(found.command))
            done.append(f"{host.label} hooks: {outcome.status} {outcome.detail}".rstrip())
    return done
