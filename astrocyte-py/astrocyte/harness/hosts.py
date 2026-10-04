"""Agent harnesses Astrocyte can register its MCP server with.

Each host knows where its harness keeps MCP registrations, how to read the
current ``astrocyte`` entry, and how to write one. Writes go through the
harness's own CLI where it has one (``claude``, ``codex``, ``gemini``) — the
CLI owns its file format and locking, and ``~/.claude.json`` in particular is
rewritten constantly by a running Claude Code. JSON-configured harnesses
(Cursor, Windsurf, Copilot CLI) are edited directly, atomically, refusing to
touch a file that is not valid JSON.

Every write is verified by re-reading the harness's config: CLI exit codes
are not trustworthy here (``claude mcp add`` exits 0 and changes nothing when
the name already exists), and an unverified "installed" is the failure mode
this whole module exists to prevent.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tomllib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SERVER_NAME = "astrocyte"

Status = Literal["installed", "updated", "unchanged", "removed", "absent", "planned", "skipped", "failed"]


_VERB = {"installed": "install", "updated": "update"}


class HostConfigError(Exception):
    """A harness config file exists but cannot be read safely."""


@dataclass(frozen=True)
class ServerSpec:
    """The MCP server entry setup registers: an absolute command and args."""

    command: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class Registration:
    """What a harness currently has registered under ``astrocyte``."""

    command: str
    args: tuple[str, ...]

    def matches(self, spec: ServerSpec) -> bool:
        return self.command == spec.command and self.args == spec.args


@dataclass(frozen=True)
class Outcome:
    host: str
    status: Status
    detail: str = ""


def _home() -> Path:
    # $HOME on POSIX; USERPROFILE on Windows (which ignores HOME, as do the
    # agents there: their configs live under the profile even when Git Bash
    # sets HOME elsewhere).
    return Path.home()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        raise HostConfigError(f"{path} is not valid JSON ({e}); fix it by hand, then re-run") from e
    if not isinstance(data, dict):
        raise HostConfigError(f"{path} is not a JSON object; fix it by hand, then re-run")
    return data


def edit_json(path: Path, change: Any) -> None:
    """Apply ``change(data)`` to a JSON file: refuses invalid JSON before
    writing anything, keeps a one-time backup of the user's original, writes
    atomically, and preserves the file's permissions."""
    data = _read_json(path)
    change(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".astrocyte-bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".astrocyte-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)  # atomic: a reader never sees a half-written file


def _registration_from(entry: Any) -> Registration | None:
    if not isinstance(entry, dict) or not entry.get("command"):
        return None
    return Registration(command=str(entry["command"]), args=tuple(str(a) for a in entry.get("args") or ()))


class Host(ABC):
    key: str
    label: str
    # A consent gate the harness applies to new MCP servers, which setup
    # cannot (and should not) bypass. Shown after a fresh install so "it
    # doesn't work in X" is never the first experience.
    next_step: str = ""

    @abstractmethod
    def detected(self) -> bool:
        """Is this harness installed for the current user?"""

    @abstractmethod
    def config_file(self) -> Path:
        """The file holding this harness's MCP registrations."""

    @abstractmethod
    def registration(self) -> Registration | None:
        """The current ``astrocyte`` entry; raises HostConfigError if unreadable."""

    @abstractmethod
    def _write(self, spec: ServerSpec) -> None: ...

    @abstractmethod
    def _remove(self) -> None: ...

    def install(self, spec: ServerSpec, *, dry_run: bool = False) -> Outcome:
        try:
            current = self.registration()
        except HostConfigError as e:
            return Outcome(self.label, "failed", str(e))
        if current is not None and current.matches(spec):
            return Outcome(self.label, "unchanged", str(self.config_file()))
        verb: Status = "updated" if current is not None else "installed"
        if dry_run:
            return Outcome(self.label, "planned", f"would {_VERB[verb]} {self.config_file()}")
        try:
            self._write(spec)
            after = self.registration()
        except (HostConfigError, OSError, subprocess.SubprocessError) as e:
            return Outcome(self.label, "failed", str(e))
        if after is None or not after.matches(spec):
            return Outcome(self.label, "failed", f"wrote {self.config_file()} but the entry did not take effect")
        return Outcome(self.label, verb, str(self.config_file()))

    def uninstall(self, *, dry_run: bool = False) -> Outcome:
        try:
            if self.registration() is None:
                return Outcome(self.label, "absent", str(self.config_file()))
            if dry_run:
                return Outcome(self.label, "planned", f"would remove from {self.config_file()}")
            self._remove()
            if self.registration() is not None:
                return Outcome(self.label, "failed", f"entry still present in {self.config_file()}")
        except (HostConfigError, OSError, subprocess.SubprocessError) as e:
            return Outcome(self.label, "failed", str(e))
        return Outcome(self.label, "removed", str(self.config_file()))


# ── CLI-managed harnesses ────────────────────────────────────────────────


class _CliHost(Host):
    """Writes through the harness's own CLI; reads the file it maintains."""

    binary: str

    def _cli(self) -> str:
        found = shutil.which(self.binary)
        if not found:
            raise HostConfigError(
                f"`{self.binary}` is not on PATH, so its MCP config can't be edited safely. "
                f"Add it to PATH and re-run, or register manually: {self.manual_command()}"
            )
        return found

    def _run(self, *args: str, check: bool = True) -> None:
        proc = subprocess.run(
            [self._cli(), *args],
            capture_output=True,
            text=True,
            timeout=60,
            # Gemini's `mcp add` writes project scope relative to the cwd;
            # pinning cwd to HOME keeps stray project files out of repos.
            cwd=str(_home()),
        )
        if check and proc.returncode != 0:
            raise subprocess.SubprocessError(
                f"`{self.binary} {' '.join(args)}` exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[:300]}"
            )

    @abstractmethod
    def manual_command(self, spec: ServerSpec | None = None) -> str: ...


# ── lifecycle hooks (automatic memory) ───────────────────────────────────


class HookHost:
    """Installs ``astrocyte hook`` commands into a harness's hooks file.

    Claude Code and Codex share one layout — ``{"hooks": {Event: [{"hooks":
    [entry]}]}}`` — and one input/output contract, so one installer serves
    both. Entries are recognised by command, never by position: the user's
    own hooks in the same file are left exactly as they were.
    """

    label: str
    #: Event → (``astrocyte hook`` subcommand, extra entry fields).
    HOOK_EVENTS: dict[str, tuple[str, dict[str, Any]]]
    #: ``--host`` passed to ``astrocyte hook``; None for Claude Code, whose
    #: hooks predate the flag (keeps existing installs "unchanged").
    hook_dialect: str | None = None
    #: A consent gate the harness applies to new hooks (cf. Host.next_step).
    hooks_next_step: str = ""
    #: Do this harness's hooks save finished turns, or only recall?
    captures_turns: bool = True
    # Matches the current ``<python> -I -m astrocyte.cli hook …`` form and the
    # earlier ``astrocyte hook …`` console-script form, with or without --host.
    _OURS = re.compile(
        r"astrocyte(?:\.cli)?['\"]?\s+hook\s+(session-start|prompt|stop)(?:\s+--host\s+[a-z-]+)?\s*$"
    )

    @abstractmethod
    def hooks_file(self) -> Path: ...

    @classmethod
    def _is_ours(cls, hook: Any) -> bool:
        return isinstance(hook, dict) and bool(cls._OURS.search(str(hook.get("command", ""))))

    def hook_commands(self) -> dict[str, str | None]:
        """Our installed hook command per event (None where absent)."""
        hooks = _read_json(self.hooks_file()).get("hooks", {})
        found: dict[str, str | None] = dict.fromkeys(self.HOOK_EVENTS)
        if not isinstance(hooks, dict):
            return found
        for event in self.HOOK_EVENTS:
            for group in hooks.get(event) or []:
                for hook in (group or {}).get("hooks") or []:
                    if self._is_ours(hook):
                        found[event] = hook["command"]
        return found

    def _wanted(self, prefix: str) -> dict[str, dict[str, Any]]:
        """``prefix`` is a ready-quoted command, e.g. ``'/py' -I -m astrocyte.cli``."""
        flag = f" --host {self.hook_dialect}" if self.hook_dialect else ""
        return {
            event: {"type": "command", "command": f"{prefix} hook {sub}{flag}", **extra}
            for event, (sub, extra) in self.HOOK_EVENTS.items()
        }

    def _strip_ours(self, data: dict) -> None:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return
        for event in list(hooks):
            if not isinstance(hooks[event], list):
                continue  # not an event (e.g. Codex's per-hook trust state)
            groups = []
            for group in hooks.get(event) or []:
                kept = [h for h in (group or {}).get("hooks") or [] if not self._is_ours(h)]
                if kept:
                    groups.append({**group, "hooks": kept})
            if groups:
                hooks[event] = groups
            else:
                hooks.pop(event)
        if not hooks:
            data.pop("hooks")

    def install_hooks(self, prefix: str, *, dry_run: bool = False) -> Outcome:
        label = f"{self.label} hooks"
        wanted = self._wanted(prefix)
        try:
            current = self.hook_commands()
        except HostConfigError as e:
            return Outcome(label, "failed", str(e))
        if all(current[e] == wanted[e]["command"] for e in wanted):
            return Outcome(label, "unchanged", str(self.hooks_file()))
        verb: Status = "updated" if any(current.values()) else "installed"
        if dry_run:
            return Outcome(label, "planned", f"would {_VERB[verb]} {self.hooks_file()}")

        def change(data: dict) -> None:
            self._strip_ours(data)  # replace stale entries; never touch the user's own hooks
            hooks = data.setdefault("hooks", {})
            for event, entry in wanted.items():
                hooks.setdefault(event, []).append({"hooks": [entry]})

        try:
            edit_json(self.hooks_file(), change)
            after = self.hook_commands()
        except (HostConfigError, OSError) as e:
            return Outcome(label, "failed", str(e))
        if any(after[e] != wanted[e]["command"] for e in wanted):
            return Outcome(label, "failed", f"wrote {self.hooks_file()} but the hooks did not take effect")
        return Outcome(label, verb, str(self.hooks_file()))

    def uninstall_hooks(self, *, dry_run: bool = False) -> Outcome:
        label = f"{self.label} hooks"
        try:
            if not any(self.hook_commands().values()):
                return Outcome(label, "absent", str(self.hooks_file()))
            if dry_run:
                return Outcome(label, "planned", f"would remove from {self.hooks_file()}")
            edit_json(self.hooks_file(), self._strip_ours)
        except (HostConfigError, OSError) as e:
            return Outcome(label, "failed", str(e))
        return Outcome(label, "removed", str(self.hooks_file()))


class ClaudeCodeHost(HookHost, _CliHost):
    key, label, binary = "claude", "Claude Code", "claude"

    def _dir(self) -> Path:
        return Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") else _home() / ".claude"

    def detected(self) -> bool:
        return self._dir().is_dir() or shutil.which(self.binary) is not None

    def config_file(self) -> Path:
        # User-scope servers live in .claude.json beside (or, with
        # CLAUDE_CONFIG_DIR, inside) the config directory.
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            return Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
        return _home() / ".claude.json"

    def registration(self) -> Registration | None:
        return _registration_from(_read_json(self.config_file()).get("mcpServers", {}).get(SERVER_NAME))

    def _write(self, spec: ServerSpec) -> None:
        # `claude mcp add` will not replace an existing name (it prints
        # "already exists" and exits 0), so remove first.
        self._remove()
        self._run("mcp", "add", "--scope", "user", SERVER_NAME, "--", spec.command, *spec.args)

    def _remove(self) -> None:
        self._run("mcp", "remove", "--scope", "user", SERVER_NAME, check=False)  # exits 1 when absent

    def manual_command(self, spec: ServerSpec | None = None) -> str:
        tail = f"{spec.command} {' '.join(spec.args)}" if spec else "<astrocyte-mcp> --config <path>"
        return f"claude mcp add --scope user {SERVER_NAME} -- {tail}"

    #: Stop is deliberately *not* async: `claude -p` exits as soon as it prints
    #: its answer and takes background hooks with it, so an async capture never
    #: ran in headless use (observed). The hook only spools the turn — ~0.35 s,
    #: after the response is already on screen.
    HOOK_EVENTS = {
        "SessionStart": ("session-start", {"timeout": 15}),
        "UserPromptSubmit": ("prompt", {"timeout": 5}),
        "Stop": ("stop", {"timeout": 10}),
    }

    def hooks_file(self) -> Path:
        return self._dir() / "settings.json"





class CodexHost(HookHost, _CliHost):
    key, label, binary = "codex", "Codex CLI", "codex"

    def _dir(self) -> Path:
        return Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else _home() / ".codex"

    def detected(self) -> bool:
        return self._dir().is_dir() or shutil.which(self.binary) is not None

    def config_file(self) -> Path:
        return self._dir() / "config.toml"

    def registration(self) -> Registration | None:
        path = self.config_file()
        if not path.exists():
            return None
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise HostConfigError(f"{path} is not valid TOML ({e}); fix it by hand, then re-run") from e
        return _registration_from(data.get("mcp_servers", {}).get(SERVER_NAME))

    def _write(self, spec: ServerSpec) -> None:
        self._remove()
        self._run("mcp", "add", SERVER_NAME, "--", spec.command, *spec.args)

    def _remove(self) -> None:
        self._run("mcp", "remove", SERVER_NAME, check=False)

    def manual_command(self, spec: ServerSpec | None = None) -> str:
        tail = f"{spec.command} {' '.join(spec.args)}" if spec else "<astrocyte-mcp> --config <path>"
        return f"codex mcp add {SERVER_NAME} -- {tail}"

    # Codex speaks Claude Code's hook contract (same events, same
    # ``hookSpecificOutput.additionalContext``; verified on codex-cli 0.160),
    # but its Stop payload carries the reply as ``last_assistant_message``
    # rather than a transcript we can parse — hence its own dialect.
    # Timeouts are seconds.
    HOOK_EVENTS = {
        "SessionStart": ("session-start", {"timeout": 15}),
        "UserPromptSubmit": ("prompt", {"timeout": 5}),
        "Stop": ("stop", {"timeout": 10}),
    }
    hook_dialect = "codex"
    hooks_next_step = (
        "Codex runs new or changed hooks only once you trust them: in Codex, run /hooks and trust "
        "the three astrocyte hooks."
    )

    # Codex reads hooks from hooks.json and from config.toml, and warns on
    # every run when both define some ("prefer a single representation"). So
    # ours go where the user's hooks already are: hooks.json only if it holds
    # hooks of theirs, otherwise config.toml — between marker comments, the
    # convention other tools writing that file use, so it can be removed
    # exactly. Every TOML edit is verified structurally: the file must parse
    # to the old contents plus (or minus) our entries, and nothing else.
    _BEGIN = "# >>> astrocyte hooks >>>"
    _END = "# <<< astrocyte hooks <<<"

    def _json_file(self) -> Path:
        return self._dir() / "hooks.json"

    def _toml(self) -> tuple[str, dict[str, Any]]:
        path = self.config_file()
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            return text, tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise HostConfigError(f"{path} is not valid TOML ({e}); fix it by hand, then re-run") from e

    @staticmethod
    def _entries(data: dict[str, Any]) -> list[Any]:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return []
        return [h for groups in hooks.values() if isinstance(groups, list)
                for group in groups for h in (group or {}).get("hooks") or []]

    def _has_foreign_hooks(self, data: dict[str, Any]) -> bool:
        return any(not self._is_ours(h) for h in self._entries(data))

    def _holds_ours(self, data: dict[str, Any]) -> bool:
        return any(self._is_ours(h) for h in self._entries(data))

    def hooks_file(self) -> Path:
        """Where our hooks are, or would be installed."""
        return self._json_file() if self._has_foreign_hooks(_read_json(self._json_file())) else self.config_file()

    def hook_commands(self) -> dict[str, str | None]:
        found: dict[str, str | None] = dict.fromkeys(self.HOOK_EVENTS)
        for data in (_read_json(self._json_file()), self._toml()[1]):
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                continue
            for event in self.HOOK_EVENTS:
                for group in hooks.get(event) or []:
                    for hook in (group or {}).get("hooks") or []:
                        if self._is_ours(hook):
                            found[event] = hook["command"]
        return found

    def _unmarked(self, text: str) -> str:
        """``text`` without our marked block (and the blank line before it)."""
        if self._BEGIN not in text:
            return text
        head, _, rest = text.partition(self._BEGIN)
        _, sep, tail = rest.partition(self._END)
        if not sep:
            raise HostConfigError(f"{self.config_file()}: '{self._BEGIN}' has no closing marker; fix it by hand")
        return head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")

    def _block(self, wanted: dict[str, dict[str, Any]]) -> str:
        lines = [self._BEGIN, "# Automatic memory, managed by `astrocyte setup` (remove: astrocyte setup --no-hooks)."]
        for event, entry in wanted.items():
            lines += ["", f"[[hooks.{event}]]", "", f"[[hooks.{event}.hooks]]"]
            # JSON string escapes are valid TOML basic-string escapes.
            lines += [f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in entry.items()]
        return "\n".join([*lines, self._END]) + "\n"

    def _write_toml(self, text: str, expected: dict[str, Any]) -> None:
        path = self.config_file()
        try:
            after = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            after = {"<unparseable>": str(e)}
        if after != expected:
            raise HostConfigError(
                f"refusing to edit {path}: the change would alter more than Astrocyte's hooks "
                "(hand-edited marker block, or hook events written as inline arrays?). "
                "Fix it by hand, or move your hooks into ~/.codex/hooks.json, then re-run"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_name(path.name + ".astrocyte-bak")
            if not backup.exists():
                shutil.copy2(path, backup)
        tmp = path.with_name(path.name + ".astrocyte-tmp")
        tmp.write_text(text, encoding="utf-8")
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)

    def _strip_json(self) -> None:
        path = self._json_file()
        data = _read_json(path)
        if not self._holds_ours(data):
            return
        self._strip_ours(data)
        if data == {}:
            path.unlink()  # held nothing but ours; even an empty file is a second hooks source
        else:
            edit_json(path, self._strip_ours)

    def _strip_toml(self) -> None:
        text, data = self._toml()
        if not self._holds_ours(data):
            return
        expected = copy.deepcopy(data)
        self._strip_ours(expected)
        self._write_toml(self._unmarked(text), expected)

    def install_hooks(self, prefix: str, *, dry_run: bool = False) -> Outcome:
        label = f"{self.label} hooks"
        wanted = self._wanted(prefix)
        try:
            current = self.hook_commands()
            target = self.hooks_file()
            text, data = self._toml()
            # Where ours are now; "unchanged" also requires the right place.
            held = {self._json_file()} if self._holds_ours(_read_json(self._json_file())) else set()
            if self._holds_ours(data):
                held.add(self.config_file())
            settled = held == {target} and (target == self._json_file() or self._BEGIN in text)
        except HostConfigError as e:
            return Outcome(label, "failed", str(e))
        if settled and all(current[e] == wanted[e]["command"] for e in wanted):
            return Outcome(label, "unchanged", str(target))
        verb: Status = "updated" if any(current.values()) else "installed"
        if dry_run:
            return Outcome(label, "planned", f"would {_VERB[verb]} {target}")
        try:
            if target == self._json_file():
                self._strip_toml()
                return super().install_hooks(prefix)  # hooks.json: the shared JSON installer
            self._strip_json()
            expected = copy.deepcopy(data)
            self._strip_ours(expected)
            hooks = expected.setdefault("hooks", {})
            for event, entry in wanted.items():
                hooks.setdefault(event, []).append({"hooks": [entry]})
            body = self._unmarked(text)
            body = body.rstrip("\n") + "\n\n" if body.strip() else ""
            self._write_toml(body + self._block(wanted), expected)
            after = self.hook_commands()
        except (HostConfigError, OSError) as e:
            return Outcome(label, "failed", str(e))
        if any(after[e] != wanted[e]["command"] for e in wanted):
            return Outcome(label, "failed", f"wrote {target} but the hooks did not take effect")
        return Outcome(label, verb, str(target))

    def uninstall_hooks(self, *, dry_run: bool = False) -> Outcome:
        label = f"{self.label} hooks"
        try:
            if not any(self.hook_commands().values()):
                return Outcome(label, "absent", str(self.hooks_file()))
            if dry_run:
                return Outcome(label, "planned", f"would remove from {self.hooks_file()}")
            self._strip_json()
            self._strip_toml()
        except (HostConfigError, OSError) as e:
            return Outcome(label, "failed", str(e))
        return Outcome(label, "removed", str(self.hooks_file()))


class GeminiHost(_CliHost):
    key, label, binary = "gemini", "Gemini CLI", "gemini"
    next_step = "Gemini CLI only loads MCP servers in trusted folders: trust your project folder when prompted."

    def detected(self) -> bool:
        return (_home() / ".gemini").is_dir() or shutil.which(self.binary) is not None

    def config_file(self) -> Path:
        return _home() / ".gemini" / "settings.json"

    def registration(self) -> Registration | None:
        return _registration_from(_read_json(self.config_file()).get("mcpServers", {}).get(SERVER_NAME))

    def _write(self, spec: ServerSpec) -> None:
        # --scope user is essential: gemini defaults to *project* scope, which
        # writes .gemini/settings.json into whatever directory you ran from.
        # `--` keeps our server's --config from being parsed as a gemini flag.
        self._remove()
        self._run("mcp", "add", "--scope", "user", SERVER_NAME, spec.command, "--", *spec.args)

    def _remove(self) -> None:
        self._run("mcp", "remove", "--scope", "user", SERVER_NAME, check=False)

    def manual_command(self, spec: ServerSpec | None = None) -> str:
        cmd, args = (spec.command, " ".join(spec.args)) if spec else ("<astrocyte-mcp>", "--config <path>")
        return f"gemini mcp add --scope user {SERVER_NAME} {cmd} -- {args}"


# ── JSON-configured harnesses ────────────────────────────────────────────


class _JsonHost(Host):
    """Harnesses with no MCP CLI: edit ``mcpServers`` in a JSON file."""

    def _entry(self, spec: ServerSpec) -> dict[str, Any]:
        return {"command": spec.command, "args": list(spec.args)}

    def registration(self) -> Registration | None:
        servers = _read_json(self.config_file()).get("mcpServers", {})
        if not isinstance(servers, dict):
            raise HostConfigError(f'{self.config_file()}: "mcpServers" must be a JSON object')
        return _registration_from(servers.get(SERVER_NAME))

    def _mutate(self, change: Any) -> None:
        edit_json(self.config_file(), lambda data: change(data.setdefault("mcpServers", {})))

    def _write(self, spec: ServerSpec) -> None:
        self._mutate(lambda servers: servers.__setitem__(SERVER_NAME, self._entry(spec)))

    def _remove(self) -> None:
        self._mutate(lambda servers: servers.pop(SERVER_NAME, None))


class CursorHost(_JsonHost):
    key, label = "cursor", "Cursor"
    next_step = "Cursor asks before loading a new MCP server: enable 'astrocyte' under Settings → MCP."

    def detected(self) -> bool:
        return (_home() / ".cursor").is_dir()

    def config_file(self) -> Path:
        return _home() / ".cursor" / "mcp.json"


class WindsurfHost(_JsonHost):
    key, label = "windsurf", "Windsurf"
    next_step = "Windsurf reads MCP config on refresh: press Refresh in the Cascade MCP panel."

    def detected(self) -> bool:
        return (_home() / ".codeium" / "windsurf").is_dir()

    def config_file(self) -> Path:
        return _home() / ".codeium" / "windsurf" / "mcp_config.json"


class _OwnHookFile(HookHost):
    """Hook layouts other than Claude Code's grouped one. Subclasses say how
    the ``astrocyte`` entries sit in their file; install / uninstall /
    verification are shared."""

    _COMMAND_KEY = "command"

    def _entries(self, data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _write_entries(self, data: dict[str, Any], entries: dict[str, dict[str, Any]]) -> None:
        raise NotImplementedError

    def _drop_entries(self, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def _hook_entry(self, command: str, extra: dict[str, Any]) -> dict[str, Any]:
        return {"type": "command", self._COMMAND_KEY: command, **extra}

    def _our_handlers(self) -> dict[str, dict[str, Any] | None]:
        events = self._entries(_read_json(self.hooks_file()))
        found: dict[str, dict[str, Any] | None] = dict.fromkeys(self.HOOK_EVENTS)
        for event in self.HOOK_EVENTS:
            for handler in events.get(event) or []:
                if isinstance(handler, dict) and self._OURS.search(str(handler.get(self._COMMAND_KEY, ""))):
                    found[event] = handler
        return found

    def hook_commands(self) -> dict[str, str | None]:
        return {event: h[self._COMMAND_KEY] if h else None for event, h in self._our_handlers().items()}

    def install_hooks(self, prefix: str, *, dry_run: bool = False) -> Outcome:
        label = f"{self.label} hooks"
        flag = f" --host {self.hook_dialect}" if self.hook_dialect else ""
        wanted = {event: self._hook_entry(f"{prefix} hook {sub}{flag}", extra)
                  for event, (sub, extra) in self.HOOK_EVENTS.items()}
        try:
            current = self._our_handlers()
        except HostConfigError as e:
            return Outcome(label, "failed", str(e))
        # Whole entries, not just commands: a field added in an upgrade (Copilot's
        # `powershell`) must reach existing installs.
        if all(current[e] == wanted[e] for e in wanted):
            return Outcome(label, "unchanged", str(self.hooks_file()))
        verb: Status = "updated" if any(current.values()) else "installed"
        if dry_run:
            return Outcome(label, "planned", f"would {_VERB[verb]} {self.hooks_file()}")
        try:
            edit_json(self.hooks_file(), lambda data: self._write_entries(data, wanted))
            after = self.hook_commands()
        except (HostConfigError, OSError) as e:
            return Outcome(label, "failed", str(e))
        if any(after[e] != wanted[e][self._COMMAND_KEY] for e in wanted):
            return Outcome(label, "failed", f"wrote {self.hooks_file()} but the hooks did not take effect")
        return Outcome(label, verb, str(self.hooks_file()))

    def uninstall_hooks(self, *, dry_run: bool = False) -> Outcome:
        label = f"{self.label} hooks"
        try:
            if not any(self.hook_commands().values()):
                return Outcome(label, "absent", str(self.hooks_file()))
            if dry_run:
                return Outcome(label, "planned", f"would remove from {self.hooks_file()}")
            edit_json(self.hooks_file(), self._drop_entries)
        except (HostConfigError, OSError) as e:
            return Outcome(label, "failed", str(e))
        return Outcome(label, "removed", str(self.hooks_file()))


class AntigravityHost(_OwnHookFile, _JsonHost):
    """Google Antigravity: the app and the ``agy`` CLI share ~/.gemini/config.

    Hook names are the top-level keys of hooks.json, so ours is one named
    entry — added and removed whole, beside the user's own. Antigravity has
    no prompt-submitted event: PreInvocation (before each model call) carries
    recall, and the project summary on a conversation's first call. Timeouts
    are seconds. Verified against agy 1.2: both hooks fire and injected
    context lands in the transcript.
    """

    key, label = "antigravity", "Antigravity"
    next_step = "Antigravity loads MCP servers and hooks when a conversation starts: open a new one."
    HOOK_EVENTS = {
        "PreInvocation": ("prompt", {"timeout": 15}),
        "Stop": ("stop", {"timeout": 10}),
    }
    hook_dialect = "antigravity"
    _HOOK_NAME = "astrocyte"

    def _dir(self) -> Path:
        return _home() / ".gemini" / "config"

    def detected(self) -> bool:
        return self._dir().is_dir() or shutil.which("agy") is not None

    def config_file(self) -> Path:
        return self._dir() / "mcp_config.json"

    def hooks_file(self) -> Path:
        return self._dir() / "hooks.json"

    def _entries(self, data: dict[str, Any]) -> dict[str, Any]:
        entry = data.get(self._HOOK_NAME)
        return entry if isinstance(entry, dict) else {}

    def _write_entries(self, data: dict[str, Any], entries: dict[str, dict[str, Any]]) -> None:
        data[self._HOOK_NAME] = {event: [handler] for event, handler in entries.items()}

    def _drop_entries(self, data: dict[str, Any]) -> None:
        data.pop(self._HOOK_NAME, None)


class CopilotHost(_OwnHookFile, _JsonHost):
    """Copilot CLI reads every ~/.copilot/hooks/*.json; ours is its own file.

    Session start injects the project summary and each prompt is recalled
    against. Turns are not captured yet: agentStop gives only a transcript
    path whose format has not been verified. Built from GitHub's hooks
    reference (not exercised against a live Copilot CLI).
    """

    key, label = "copilot", "Copilot CLI"
    captures_turns = False
    HOOK_EVENTS = {
        "sessionStart": ("session-start", {"timeoutSec": 15}),
        "userPromptSubmitted": ("prompt", {"timeoutSec": 5}),
    }
    hook_dialect = "copilot"
    _COMMAND_KEY = "bash"

    def _hook_entry(self, command: str, extra: dict[str, Any]) -> dict[str, Any]:
        # Copilot runs `bash` on macOS/Linux and `powershell` on Windows; the
        # command is written to mean the same in both (server.hook_prefix).
        return {"type": "command", "bash": command, "powershell": command, **extra}

    def hooks_file(self) -> Path:
        return self._dir() / "hooks" / "astrocyte.json"

    def _entries(self, data: dict[str, Any]) -> dict[str, Any]:
        hooks = data.get("hooks")
        return hooks if isinstance(hooks, dict) else {}

    def _write_entries(self, data: dict[str, Any], entries: dict[str, dict[str, Any]]) -> None:
        data.clear()
        data.update({"version": 1, "hooks": {event: [handler] for event, handler in entries.items()}})

    def _drop_entries(self, data: dict[str, Any]) -> None:
        data.clear()

    def uninstall_hooks(self, *, dry_run: bool = False) -> Outcome:
        outcome = super().uninstall_hooks(dry_run=dry_run)
        if outcome.status == "removed":
            self.hooks_file().unlink(missing_ok=True)  # the file was only ever ours
            self.hooks_file().with_name(self.hooks_file().name + ".astrocyte-bak").unlink(missing_ok=True)
        return outcome

    def _dir(self) -> Path:
        return Path(os.environ["COPILOT_HOME"]) if os.environ.get("COPILOT_HOME") else _home() / ".copilot"

    def detected(self) -> bool:
        return self._dir().is_dir()

    def config_file(self) -> Path:
        return self._dir() / "mcp-config.json"

    def _entry(self, spec: ServerSpec) -> dict[str, Any]:
        # Copilot CLI requires an explicit type and tool allow-list.
        return {"type": "local", "command": spec.command, "args": list(spec.args), "tools": ["*"]}


ALL_HOSTS: tuple[type[Host], ...] = (ClaudeCodeHost, CodexHost, CursorHost, GeminiHost, WindsurfHost, CopilotHost)

# Every harness setup supports. ALL_HOSTS keeps its v0.16.0 value (it is part
# of the public API); hosts added since are appended here.
SUPPORTED_HOSTS: tuple[type[Host], ...] = (*ALL_HOSTS, AntigravityHost)


def hosts() -> list[Host]:
    return [cls() for cls in SUPPORTED_HOSTS]


def host_by_key(key: str) -> Host:
    for h in hosts():
        if h.key == key:
            return h
    raise KeyError(f"unknown harness {key!r}; expected one of {[c.key for c in SUPPORTED_HOSTS]}")
