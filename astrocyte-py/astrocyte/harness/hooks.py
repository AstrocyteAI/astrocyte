"""``astrocyte hook <event> [--host codex]`` — agent lifecycle hooks for automatic memory.

=================  ======================================================
``session-start``  Starts the agent daemon (waits for it: once per session)
                   and injects a short summary of this project's memory.
``prompt``         Injects memories relevant to the prompt — gated on
                   similarity, deduplicated per session, hard deadline. A
                   trivial prompt never reaches the daemon at all.
``stop``           Spools the turn that just finished (~0.35 s; synchronous,
                   because headless `claude -p` kills background hooks).
=================  ======================================================

Claude Code and Codex fire these under the same event names with the same
input and output contract. They differ in where the finished turn comes from:
Claude Code's Stop points at a transcript we read incrementally; Codex's Stop
carries the reply itself (``last_assistant_message``), so the prompt is kept
from ``prompt`` until the turn ends.

Contract with the agent: always exit 0, print nothing but valid hook JSON,
never load a model in this process, never wait past the deadline. A broken or
missing daemon means "no memory this turn", never a blocked prompt.

Interactive sessions only, by default. Hooks are global, so they also fire for
every headless ``claude -p`` / ``codex exec`` on the machine: scripts,
benchmarks, and LLM provider calls (including Astrocyte's own ``claude_cli``
provider). Capturing those pollutes memory, injecting into them perturbs
automated runs, and the provider case recursed (observed on first real
install). A session whose agent process runs non-interactively is left alone.

``ASTROCYTE_HOOKS=off`` pauses everything; ``ASTROCYTE_HOOKS=all`` also
serves headless sessions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from astrocyte.policy.barriers import redact_secrets

from . import agentd
from .paths import config_path, state_dir
from .project import project_bank
from .transcript import Turn, antigravity_prompt, read_new_turns

SESSION_START_WAIT = 8.0  # cold start incl. model load measured at ~1.5 s
PROMPT_DEADLINE = 2.5  # the user is waiting; warm recall measured at ~10 ms

_WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-/]+")
_SMALL_TALK = frozenset(
    "the a an and or but if is are was were be to of in on at for with this that it its "
    "please thanks thank you ok okay yes no sure great good nice cool lgtm continue go ahead "
    "do it can could would should will i me my we our us hi hello hey".split()
)

logger = logging.getLogger("astrocyte.hooks")


def worth_recalling(prompt: str) -> bool:
    """Skip the daemon for prompts with nothing to recall against —
    "thanks!", "continue", slash commands."""
    text = prompt.strip()
    if not text or text.startswith("/"):
        return False
    terms = [w for w in _WORD.findall(text.lower()) if w not in _SMALL_TALK and len(w) > 2]
    return len(terms) >= 2


# Codex options that consume the next argument, so ``codex -m o3 exec`` is
# read as subcommand ``exec`` rather than ``o3``.
_CODEX_VALUE_FLAGS = frozenset(
    "-c --config -m --model -p --profile -s --sandbox -a --ask-for-approval -C --cd -i --image "
    "--enable --disable --add-dir --local-provider".split()
)
# `app-server` is deliberately absent: it backs Codex's desktop app and IDE
# extension, which are interactive.
_CODEX_HEADLESS = frozenset({"exec", "e", "review", "mcp-server"})


def _claude_headless(argv: list[str]) -> bool:
    # The desktop app and the interactive CLI never pass -p/--print
    # (verified against the desktop app's own invocation).
    return any(a in ("-p", "--print") for a in argv[1:])


def _codex_headless(argv: list[str]) -> bool:
    args = iter(argv[1:])
    for arg in args:
        if arg in _CODEX_VALUE_FLAGS:
            next(args, None)
        elif not arg.startswith("-"):
            return arg in _CODEX_HEADLESS
    return False


def _print_flag_headless(argv: list[str]) -> bool:
    """agy and copilot: -p / --print / --prompt run one prompt and exit."""
    return any(a in ("-p", "--print", "--prompt") for a in argv[1:])


def _claude_emit(event: str, context: str) -> None:
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}))


def _copilot_emit(_event: str, context: str) -> None:
    if context:
        print(json.dumps({"additionalContext": context}))


def _antigravity_emit(_event: str, context: str) -> None:
    if context:  # main() prints {} when nothing was emitted
        print(json.dumps({"injectSteps": [{"ephemeralMessage": context}]}))


@dataclass(frozen=True)
class Dialect:
    """How one harness speaks the hook contract."""

    source: str  # recorded on every captured memory
    binary: str  # process name of the agent, found above the hook
    headless: Callable[[list[str]], bool]  # agent argv → runs non-interactively?
    # Where the finished turn comes from at Stop: Claude Code's transcript,
    # or the payload itself (the prompt kept from UserPromptSubmit).
    turn_from_payload: bool
    emit: Callable[[str, str], None] = _claude_emit
    # Antigravity has no prompt-submitted event: its PreInvocation hook runs
    # before every model call and carries no prompt, so the prompt is read
    # from the transcript and recall runs once per new user message.
    prompt_from_transcript: bool = False
    antigravity_transcript: bool = False
    captures_turns: bool = True  # False: this agent's hooks only recall

    @property
    def turn_source(self) -> str | None:
        """"payload", "transcript", or None when turns aren't captured."""
        if not self.captures_turns:
            return None
        return "payload" if self.turn_from_payload else "transcript"


DIALECTS: dict[str, Dialect] = {
    "claude": Dialect("claude-code", "claude", _claude_headless, turn_from_payload=False),
    "codex": Dialect("codex", "codex", _codex_headless, turn_from_payload=True),
}

# Added after v0.16.0. Kept out of DIALECTS, whose released value is part of
# the public API; look dialects up with dialect_for().
ANTIGRAVITY_DIALECT = Dialect(
    "antigravity", "agy", _print_flag_headless, turn_from_payload=False,
    emit=_antigravity_emit, prompt_from_transcript=True, antigravity_transcript=True,
)
COPILOT_DIALECT = Dialect(
    "copilot", "copilot", _print_flag_headless, turn_from_payload=False, emit=_copilot_emit, captures_turns=False,
)
_MORE_DIALECTS = {"antigravity": ANTIGRAVITY_DIALECT, "copilot": COPILOT_DIALECT}


def dialect_for(host: str) -> Dialect | None:
    """The hook dialect of ``host`` (``astrocyte hook --host``), if known."""
    return DIALECTS.get(host) or _MORE_DIALECTS.get(host)


def _agent_ancestor_args(binary: str, max_depth: int = 5) -> list[str] | None:
    """argv of the nearest ``binary`` process above this hook, if any.

    Hooks may be run through a shell, so walk a few levels up. Environment
    variables can't answer "is this headless?": a ``claude -p`` started from
    an interactive session inherits ``CLAUDE_CODE_SESSION_ATTENDED=1`` and
    ``CLAUDE_CODE_ENTRYPOINT`` unchanged (measured).
    """
    pid = os.getppid()
    for _ in range(max_depth):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-o", "args=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if not out:
            return None
        ppid, _, args = out.partition(" ")
        argv = args.split()
        if argv and binary in Path(argv[0]).name.lower():
            return argv
        # Node CLIs (copilot) run as `node /path/to/copilot …`.
        if len(argv) > 1 and Path(argv[0]).name.startswith("node") and binary in Path(argv[1]).name.lower():
            return argv[1:]
        try:
            pid = int(ppid)
        except ValueError:
            return None
    return None


def headless_session(dialect: str = "claude") -> bool:
    """Is the agent session running non-interactively (``claude -p``,
    ``codex exec``, ``agy -p``, ``copilot -p``)? The Antigravity app runs
    no ``agy`` process, so its sessions are interactive."""
    d = dialect_for(dialect)
    if d is None:
        return False
    argv = _agent_ancestor_args(d.binary)
    return bool(argv) and d.headless(argv)


def _provider_scratch(cwd: str) -> bool:
    """Provider subprocesses run in a private ``astrocyte-claude-cli-*`` temp
    dir. Older provider code didn't disable hooks, so recognise it directly."""
    return Path(cwd).name.startswith("astrocyte-claude-cli-")


def _session_file(session_id: str, suffix: str = "") -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"
    return state_dir() / "sessions" / f"{safe}{suffix}.json"


def _write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(data))


def _session_start(payload: dict, cfg: Path, bank: str, session: str, dialect: Dialect) -> None:
    if not agentd.ensure_running(cfg, wait=SESSION_START_WAIT):
        return
    reply = agentd.request(
        "boot", {"bank": bank, "session_id": session, "source": payload.get("source")}, timeout=5
    )
    dialect.emit("SessionStart", (reply or {}).get("context", ""))


def _latest_user_message(transcript: object) -> tuple[int | None, str]:
    """The last user message in an Antigravity transcript, with its step index."""
    if not isinstance(transcript, str) or not transcript:
        return None, ""
    step, text = None, ""
    try:
        with open(transcript, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(line, dict) and (prompt := antigravity_prompt(line)) is not None:
                    step, text = line.get("step_index"), prompt
    except OSError:
        return None, ""
    return step, text


def _prompt_from_transcript(payload: dict, cfg: Path, bank: str, session: str, dialect: Dialect) -> None:
    """Antigravity: PreInvocation runs before every model call of a turn, so
    act once per new user message; and since there is no session-start event,
    the project summary rides on a conversation's first model call."""
    step, prompt = _latest_user_message(payload.get("transcriptPath") or payload.get("transcript_path"))
    if step is None:
        return
    marker = _session_file(session, ".asked")
    try:
        first_sight = False
        if json.loads(marker.read_text()).get("step") == step:
            return  # a later model call in the same turn
    except (OSError, ValueError, AttributeError):
        first_sight = True
    _write_state(marker, {"step": step})
    parts: list[str] = []
    if first_sight and agentd.ensure_running(cfg, wait=SESSION_START_WAIT):
        boot = agentd.request("boot", {"bank": bank, "session_id": session}, timeout=5)
        parts.append((boot or {}).get("context", ""))
    if worth_recalling(prompt):
        reply = agentd.request("recall", {"bank": bank, "session_id": session, "prompt": prompt},
                               timeout=PROMPT_DEADLINE)
        if reply is None:
            agentd.spawn(cfg)
        else:
            parts.append(reply.get("context", ""))
    dialect.emit("UserPromptSubmit", "\n\n".join(p for p in parts if p))


def _prompt(payload: dict, cfg: Path, bank: str, session: str, dialect: Dialect) -> None:
    if dialect.prompt_from_transcript:
        return _prompt_from_transcript(payload, cfg, bank, session, dialect)
    prompt = payload.get("prompt") or ""
    if dialect.turn_source == "payload" and prompt.strip() and agentd.supported():
        # Kept for Stop, which reports only the reply. Every prompt, even
        # one too slight to recall against: it is still half of a turn.
        _write_state(_session_file(session, ".prompt"),
                     {"prompt": redact_secrets(prompt), "at": datetime.now(timezone.utc).isoformat()})
    if not worth_recalling(prompt):
        return
    reply = agentd.request("recall", {"bank": bank, "session_id": session, "prompt": prompt}, timeout=PROMPT_DEADLINE)
    if reply is None:
        agentd.spawn(cfg)  # warm for the next prompt; never make this one wait
        return
    dialect.emit("UserPromptSubmit", reply.get("context", ""))


def _noop() -> None:
    pass


# Each source returns the turns to capture and a commit that marks them taken.
# The commit runs only after the spool write succeeded: a crash in between
# captures the same turn again rather than losing it.
TurnSource = tuple[list[Turn], Callable[[], None]]


def _turns_from_transcript(payload: dict, session: str, *, antigravity: bool = False) -> TurnSource:
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    if not transcript:
        return [], _noop
    marker = _session_file(session)
    try:
        offset = int(json.loads(marker.read_text())["offset"])
        first_sight = False
    except (OSError, ValueError, KeyError, TypeError):
        offset, first_sight = 0, True
    turns, resume = read_new_turns(transcript, offset, antigravity=antigravity)
    if first_sight:
        # A session we have never seen — already running when the hooks were
        # installed (Claude Code hot-reloads settings), or resumed from before
        # — would otherwise have its entire history captured in one burst
        # (observed: 359 turns from one session in seconds). Memory starts now.
        turns = turns[-1:]
    if resume == offset:
        return turns, _noop
    return turns, lambda: _write_state(marker, {"offset": resume, "transcript": transcript})


def _turn_from_payload(payload: dict, session: str) -> TurnSource:
    pending = _session_file(session, ".prompt")

    def commit() -> None:
        pending.unlink(missing_ok=True)

    try:
        kept = json.loads(pending.read_text())
    except (OSError, ValueError):
        return [], _noop  # the prompt predates the hooks, or this turn was already taken
    answer = payload.get("last_assistant_message")
    if not isinstance(answer, str) or not answer.strip() or not isinstance(kept, dict):
        return [], commit
    try:
        started = datetime.fromisoformat(kept.get("at") or "")
    except (TypeError, ValueError):
        started = None
    return [Turn(user=str(kept.get("prompt") or ""), assistant=[answer], started_at=started)], commit


def _stop(payload: dict, cfg: Path, bank: str, session: str, dialect: Dialect) -> None:
    if not agentd.supported() or dialect.turn_source is None:
        return  # without a daemon to drain it, the spool would only grow
    if dialect.turn_source == "payload":
        turns, commit = _turn_from_payload(payload, session)
    else:
        turns, commit = _turns_from_transcript(payload, session, antigravity=dialect.antigravity_transcript)
    if turns:
        # Scrubbed before it touches disk: the retain barrier redacts again,
        # but the spool is plaintext and outlives a crashed daemon.
        agentd.spool_capture(
            bank, session, dialect.source,
            [{"content": redact_secrets(t.render()), "started_at": t.started_at.isoformat() if t.started_at else None}
             for t in turns],
        )
    commit()
    if turns and agentd.request("capture", timeout=0.5) is None:
        agentd.spawn(cfg)


_HANDLERS = {"session-start": _session_start, "prompt": _prompt, "stop": _stop}


def run(event: str, stdin_text: str, host: str = "claude") -> int:
    mode = os.environ.get("ASTROCYTE_HOOKS", "").strip().lower()
    if mode in ("0", "off", "false", "no"):
        return 0
    handler = _HANDLERS.get(event)
    dialect = dialect_for(host)
    if handler is None or dialect is None:
        return 0
    try:
        payload = json.loads(stdin_text or "{}")
        if not isinstance(payload, dict):
            return 0
        cfg = config_path()
        if not cfg.is_file():
            return 0  # not set up: stay out of the way
        workspaces = payload.get("workspacePaths")
        cwd = str(payload.get("cwd") or (workspaces[0] if isinstance(workspaces, list) and workspaces else "")
                  or os.getcwd())
        if _provider_scratch(cwd) or (mode != "all" and headless_session(host)):
            return 0
        session = str(payload.get("session_id") or payload.get("sessionId") or payload.get("conversationId")
                      or "unknown")
        bank = project_bank(cwd)
        handler(payload, cfg, bank, session, dialect)
    except Exception:  # noqa: BLE001 — a hook must never break the agent
        try:
            d = state_dir()
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            # A handler of our own, not basicConfig: that is a no-op once
            # anything has configured the root logger.
            handler = logging.FileHandler(d / "hooks.log")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            try:
                logger.exception("hook %s failed", event)
            finally:
                logger.removeHandler(handler)
                handler.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


def main(event: str, host: str = "claude") -> int:
    if host == "antigravity":
        # Antigravity parses every hook's stdout as JSON: say "nothing" with {}
        # whenever the hook had nothing to add (or stayed out of the way).
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run(event, sys.stdin.read() if not sys.stdin.isatty() else "", host)
        print(out.getvalue().strip() or "{}")
        return code
    return run(event, sys.stdin.read() if not sys.stdin.isatty() else "", host)
