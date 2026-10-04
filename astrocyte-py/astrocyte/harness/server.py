"""Locating and probing the ``astrocyte-mcp`` server setup registers."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .hosts import ServerSpec


@dataclass(frozen=True)
class ServerLocation:
    command: str
    ephemeral: bool  # inside a cache uv/pipx may garbage-collect


def locate_script(name: str) -> ServerLocation | None:
    """A console script from *this* installation, as an absolute path.

    The one beside the running interpreter wins over PATH: setup must register
    commands from the same environment it is running in, not whichever
    install happens to come first on PATH.
    """
    exe = f"{name}.exe" if os.name == "nt" else name
    beside = Path(sys.executable).parent / exe
    found = str(beside) if beside.exists() else shutil.which(name)
    if not found:
        return None
    resolved = str(Path(found).absolute())
    # `uvx astrocyte setup` runs from uv's archive cache, which uv prunes; a
    # path registered from there breaks later with no obvious cause.
    markers = (f"{os.sep}uv{os.sep}archive-", f"{os.sep}.cache{os.sep}uv{os.sep}", f"{os.sep}pipx{os.sep}.cache")
    return ServerLocation(command=resolved, ephemeral=any(m in resolved for m in markers))


def locate_mcp_server() -> ServerLocation | None:
    """The interpreter of *this* installation, which runs the server.

    Registrations name the interpreter rather than the ``astrocyte-mcp``
    console script so they can pass ``-I`` (isolated mode): agents launch
    servers and hooks with their own environment, and an inherited
    ``PYTHONPATH`` made the installed scripts import a different checkout's
    code (observed on first real install). ``-I`` also keeps the cwd off
    ``sys.path``, so working inside an Astrocyte source tree can't shadow it.
    The interpreter path is kept as-is, not resolved: in a venv it is a
    symlink, and resolving it would escape the venv's site-packages.
    """
    if locate_script("astrocyte-mcp") is None:
        return None  # not installed in this environment
    exe = str(Path(sys.executable).absolute())
    markers = (f"{os.sep}uv{os.sep}archive-", f"{os.sep}.cache{os.sep}uv{os.sep}", f"{os.sep}pipx{os.sep}.cache")
    return ServerLocation(command=exe, ephemeral=any(m in exe for m in markers))


ISOLATED = "-I"


def server_spec(python: str, config: Path) -> ServerSpec:
    return ServerSpec(command=python, args=(ISOLATED, "-m", "astrocyte.mcp", "--config", str(config)))


class HookPathError(ValueError):
    """The interpreter path can't be written as a shell-neutral hook command."""


# Characters every shell an agent may use on Windows (cmd, PowerShell, Git
# Bash) reads literally in an unquoted word.
_SHELL_NEUTRAL = re.compile(r"^[A-Za-z0-9_.~:/-]+$")


def hook_prefix(python: str, *, windows: bool = os.name == "nt") -> str:
    """Shell command prefix for ``astrocyte hook <event>`` registrations.

    POSIX: shell-quoted. Windows: an agent runs hooks through cmd, PowerShell
    or Git Bash depending on the machine (Claude Code: Git Bash when present,
    else PowerShell), and no quoting means the same in all three. So the path
    is written unquoted with forward slashes (bash keeps them; CreateProcess
    accepts them) and without spaces, using its 8.3 short form if it has any.
    """
    if not windows:
        import shlex

        return f"{shlex.quote(python)} {ISOLATED} -m astrocyte.cli"
    path = _short_path(python) if " " in python else python
    path = path.replace("\\", "/")
    if not _SHELL_NEUTRAL.match(path):
        raise HookPathError(
            f"{python} can't be written so that cmd, PowerShell and Git Bash all run it (it has spaces or shell "
            "characters, and no 8.3 short name). Reinstall under a plain path, e.g. "
            "uv tool install 'astrocyte[local]' with UV_TOOL_DIR=C:/astrocyte-tools."
        )
    return f"{path} {ISOLATED} -m astrocyte.cli"


def _short_path(path: str) -> str:
    """Windows' 8.3 alias of ``path`` (no spaces), or ``path`` where there is none."""
    try:
        import ctypes
        from ctypes import wintypes

        get = ctypes.windll.kernel32.GetShortPathNameW  # type: ignore[attr-defined]
        get.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get.restype = wintypes.DWORD
        size = get(path, None, 0)
        if not size:
            return path
        buf = ctypes.create_unicode_buffer(size)
        return buf.value if get(path, buf, size) else path
    except (AttributeError, OSError):  # not Windows, or no kernel32
        return path


@dataclass(frozen=True)
class HandshakeResult:
    ok: bool
    detail: str
    tools: tuple[str, ...] = ()
    seconds: float = 0.0


_EXCEPTION_LINE = re.compile(r"^\s*[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt)\b.*:")


def _explain(stderr: str | None) -> str:
    """The line of a crash's stderr that says what went wrong.

    A traceback's last lines are often frames or source, and a chained
    exception repeats; the final ``SomethingError: message`` line is the one
    a user can act on. Without one, the last few lines as they are.
    """
    lines = [ln.rstrip() for ln in (stderr or "").splitlines() if ln.strip()]
    errors = [ln.strip() for ln in lines if _EXCEPTION_LINE.match(ln)]
    if errors:
        return errors[-1]
    return " / ".join(ln.strip() for ln in lines[-3:])


def handshake(spec: ServerSpec, *, timeout: float = 90.0) -> HandshakeResult:
    """Launch the server exactly as a harness would and complete an MCP
    ``initialize`` + ``tools/list``. Catches what static checks cannot: a
    missing dependency, a config the server rejects, a broken interpreter.

    Each reply is awaited before the next request is sent and stdin stays open
    until the end: a stdio server may exit on EOF before answering requests
    that are still queued.
    """
    started = time.perf_counter()
    deadline = started + timeout
    try:
        proc = subprocess.Popen(
            [spec.command, *spec.args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except OSError as e:
        return HandshakeResult(False, f"could not start {spec.command}: {e}")

    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)  # EOF

    threading.Thread(target=pump, daemon=True).start()

    def send(msg: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def await_reply(want: int) -> dict | None:
        while (remaining := deadline - time.perf_counter()) > 0:
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want:
                return msg
        return None

    def failure(default: str) -> HandshakeResult:
        proc.kill()
        _, stderr = proc.communicate(timeout=10)
        return HandshakeResult(False, _explain(stderr) or default, seconds=time.perf_counter() - started)

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "astrocyte-doctor", "version": "1"}}})
        if await_reply(1) is None:
            return failure(f"no initialize reply within {timeout:.0f}s")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = await_reply(2)
        if listed is None or "result" not in listed:
            return failure("server did not answer tools/list")
    except (BrokenPipeError, OSError):
        return failure("server exited during the handshake")

    proc.kill()
    proc.communicate(timeout=10)
    tools = tuple(t.get("name", "") for t in listed["result"].get("tools", []))
    elapsed = time.perf_counter() - started
    if "memory_retain" not in tools:
        return HandshakeResult(False, "server answered but exposes no memory_retain tool", tools, elapsed)
    return HandshakeResult(True, f"{len(tools)} tools", tools, elapsed)
