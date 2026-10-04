"""``astrocyte agentd`` — warm memory for agent hooks.

Why a daemon: a hook is a fresh process per event. Measured on an Apple
Silicon laptop, the first recall in a fresh process costs ~1.1 s (mostly
importing fastembed), against 8 ms in a warm one — and ``UserPromptSubmit``
blocks the user's prompt until the hook returns. So hooks talk to this
per-user process and never load a model themselves.

Transport: a Unix domain socket in the state directory (0600: filesystem
permissions are the authentication). Where there is none (Windows; or forced
with ``ASTROCYTE_AGENTD_TRANSPORT=tcp``), TCP on 127.0.0.1 at a port the OS
picks, published with a random token in ``agentd.json`` in the state
directory; a request without that token gets no reply.

Lifecycle: started on demand by the hooks, one instance per user (a lock file),
exits after ``ASTROCYTE_AGENTD_IDLE`` seconds without a request (default
1800) or as soon as its config file changes, so the next hook restarts it on
the new config. Captures arrive through a durable on-disk spool, so a daemon
that is down or restarting loses nothing.

Relevance gating: recall's fused ``score`` is a ranking value, not a
similarity — an unrelated prompt ("weather in Paris") outranked related ones
in testing. Injection is therefore gated on the cosine between the prompt and
each candidate, computed here with the warm model. Calibrated on
bge-small-en-v1.5: relevant pairs scored ≥ 0.661 (long captured turns) and
≥ 0.706 (short facts); unrelated prompts scored ≤ 0.609. Defaults sit in that
gap; ``ASTROCYTE_INJECT_MIN_SIMILARITY`` overrides.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import logging.handlers
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import agentd_endpoint, agentd_socket, config_path, spool_dir, state_dir

logger = logging.getLogger("astrocyte.agentd")

INJECT_MIN_SIMILARITY = float(os.environ.get("ASTROCYTE_INJECT_MIN_SIMILARITY", "0.65"))
# Keep only hits close to the best one: weaker neighbours that clear the
# absolute bar were, in calibration, other topics from the same session.
INJECT_RELATIVE_WINDOW = 0.08
PROMPT_MAX_HITS = 4
PROMPT_MAX_CHARS = 1_800
BOOT_MAX_ITEMS = 8
BOOT_MAX_CHARS = 3_000
ITEM_MAX_CHARS = 360
# Where you left off: the closing turns of the project's previous session.
# The last answer gets the most room: that is where conclusions and next steps are.
RESUME_MAX_TURNS = 3
RESUME_LAST_MAX_CHARS = 700
RESUME_SCAN = 60
SOURCE_LABELS = {"claude-code": "Claude Code", "codex": "Codex", "antigravity": "Antigravity",
                 "copilot": "Copilot CLI"}
DRAIN_INTERVAL_SECONDS = 20.0
IDLE_EXIT_SECONDS = float(os.environ.get("ASTROCYTE_AGENTD_IDLE", "1800"))

# The conversation pipeline stores turns with single newlines between roles,
# whatever separator capture wrote, so match any run of whitespace.
_TURN = re.compile(r"^\*\*user\*\*:\s*(?P<q>.*?)\s*\*\*assistant\*\*:\s*(?P<a>.*)$", re.DOTALL)


TRANSPORT_ENV = "ASTROCYTE_AGENTD_TRANSPORT"


def transport() -> str:
    """``unix`` where there are Unix domain sockets, else ``tcp`` (loopback +
    token). ``ASTROCYTE_AGENTD_TRANSPORT=tcp`` forces TCP, so the Windows
    path is exercised on every platform."""
    unix = hasattr(socket, "AF_UNIX") and os.name != "nt"
    forced = os.environ.get(TRANSPORT_ENV, "").strip().lower()
    return "tcp" if forced == "tcp" or not unix else "unix"


def supported() -> bool:
    """Can hooks reach a daemon here? Every platform has a transport; kept so
    callers can still ask (and tests can say no)."""
    return True


# ── rendering ────────────────────────────────────────────────────────────


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_memory(text: str, when: datetime | None, *, limit: int = ITEM_MAX_CHARS) -> str:
    """One line per memory. Captured turns become "Q … → A …"; the answer
    gets most of the room because that is where conclusions live."""
    m = _TURN.match(text.strip())
    if m:
        q = _clip(m["q"], 110)
        body = f"Q: {q} → A: {_clip(m['a'], limit - len(q) - 12)}"
    else:
        body = _clip(text, limit)
    return f"- [{when.date().isoformat()}] {body}" if when else f"- {body}"


def _clip_left(text: str, limit: int) -> str:
    """The end of ``text``: where a long answer's conclusion is."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    tail = text[-(limit - 1):]
    return "…" + (tail.split(" ", 1)[1] if " " in tail[:40] else tail)


def _join_overlapping(a: str, b: str) -> str:
    """``a`` then ``b``, written once where the chunker made them overlap."""
    for k in range(min(len(a), len(b)), 15, -1):
        if a.endswith(b[:k]):
            return a + b[k:]
    return f"{a} {b}" if a else b


def _groups(items: list[Any]) -> list[list[Any]]:
    """Memories regrouped into what was retained together, newest first.

    A long captured turn is stored as several chunks (the question, then
    overlapping pieces of the answer) that share one ``_retain_id`` and come
    back in no useful order; ``_chunk_index`` gives the order they were written."""
    groups: dict[str, list[Any]] = {}
    for item in items:
        meta = item.metadata or {}
        # _retain_id since 0.18; _created_at alone for memories stored before
        # (it can merge retains made within one clock tick).
        key = meta.get("_retain_id") or meta.get("_created_at") or item.id
        groups.setdefault(str(key), []).append(item)
    out = []
    for group in groups.values():
        # _chunk_index where stored (since 0.18); retained_at for older chunks.
        group.sort(key=lambda i: ((i.metadata or {}).get("_chunk_index", 0), i.retained_at is None,
                                  i.retained_at or 0))
        out.append(group)
    return out


def render_group(group: list[Any], when: datetime | None, *, limit: int = ITEM_MAX_CHARS) -> str:
    """One line for memories retained together. A chunked turn shows its
    question and the end of its answer; other text, its beginning."""
    if len(group) == 1:
        return render_memory(group[0].text, when, limit=limit)
    question = next((i.text for i in group if i.text.lstrip().startswith("**user**")), None)
    if question is None:
        return render_memory(group[0].text, when, limit=limit)
    m = _TURN.match(question.strip())
    q = _clip(m["q"] if m else question.split(":", 1)[-1], 110)
    answer = ""
    for item in group:
        if item.text.lstrip().startswith("**user**"):
            answer = m["a"] if m else ""
        else:
            answer = _join_overlapping(answer, item.text.strip())
    answer = re.sub(r"^\*\*assistant\*\*:\s*", "", answer.strip())
    body = f"Q: {q} → A: {_clip_left(answer, limit - len(q) - 12)}"
    return f"- [{when.date().isoformat()}] {body}" if when else f"- {body}"


def _budget(lines: list[str], limit: int) -> list[str]:
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > limit:
            break
        out.append(line)
        used += len(line) + 1
    return out


def _ago(when: datetime, now: datetime | None = None) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    minutes = int(((now or datetime.now(timezone.utc)) - when).total_seconds() // 60)
    if minutes < 2:
        return "just now"
    if minutes < 60:
        return f"{minutes} minutes ago"
    if minutes < 48 * 60:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return f"{minutes // (24 * 60)} days ago"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ── daemon ───────────────────────────────────────────────────────────────


class AgentDaemon:
    def __init__(self, cfg_path: Path) -> None:
        from .memories import open_local

        self.cfg_path = cfg_path
        self.cfg_mtime = cfg_path.stat().st_mtime
        # No recall-time query expansion (an LLM call of 5–9 s via a CLI
        # provider; a hook on the prompt path cannot afford it) and no
        # observation consolidation (an LLM call per captured turn in the
        # background — the user's subscription, unasked; a capture burst
        # queued 32 of them).
        self.pipeline, self.brain = open_local(cfg_path)
        self.injected: dict[str, set[str]] = {}
        self.token: str | None = None  # set when serving over TCP
        self.last_activity = time.monotonic()
        self._drain_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    async def warm(self) -> None:
        """Load the embedding model before accepting requests, so the first
        prompt of a session is as fast as the rest."""
        await self.pipeline.llm_provider.embed(["warm"])

    # ── ops ───────────────────────────────────────────────────────────

    async def op_ping(self, _: dict) -> dict:
        return {"ok": True, "pid": os.getpid(), "config": str(self.cfg_path)}

    async def _previous_session(self, bank: str, session: str) -> list[list[Any]]:
        """The closing captured turns (oldest first) of the newest session in
        ``bank`` other than ``session``. Sessions running side by side
        interleave, so other sessions' turns are skipped, not a stop."""
        from astrocyte.types import VectorFilters

        recent = getattr(self.pipeline.vector_store, "list_recent_vectors", None)
        if recent is None:
            return []
        previous, turns = None, []
        items = await recent(bank, limit=RESUME_SCAN, filters=VectorFilters(tags=["captured"]))
        for group in _groups(items):
            sid = (group[0].metadata or {}).get("session_id") or ""
            if not sid or sid == session or (previous is not None and sid != previous):
                continue
            previous = sid
            turns.append(group)
            if len(turns) == RESUME_MAX_TURNS:
                break
        return turns[::-1]

    async def op_boot(self, req: dict) -> dict:
        bank, session = req["bank"], req.get("session_id") or ""
        if req.get("source") in ("clear", "compact"):
            self.injected.pop(session, None)  # earlier injections left the context
        seen = self.injected.setdefault(session, set())
        sections: list[str] = []
        used = 0
        resume = await self._previous_session(bank, session)
        if resume:
            last = resume[-1][-1]
            when = last.occurred_at or last.retained_at
            agent = SOURCE_LABELS.get(str((last.metadata or {}).get("source") or ""), "an agent")
            lines = [render_group(g, None, limit=RESUME_LAST_MAX_CHARS if g is resume[-1] else ITEM_MAX_CHARS)
                     for g in resume]
            sections.append(f"Where you left off: the previous session here ({agent}"
                            + (f", {_ago(when)}" if when else "") + ") ended with:\n" + "\n".join(lines))
            used = len(sections[0])
        shown = {item.id for g in resume for item in g}
        seen |= shown
        recent_groups: list[list[Any]] = []
        recent = getattr(self.pipeline.vector_store, "list_recent_vectors", None)
        if recent is not None:
            items = await recent(bank, limit=(BOOT_MAX_ITEMS + RESUME_MAX_TURNS) * 4)
            recent_groups = [g for g in _groups(items) if not shown & {i.id for i in g}][:BOOT_MAX_ITEMS]  # not twice
        body = _budget([render_group(g, g[0].occurred_at or g[0].retained_at) for g in recent_groups],
                       BOOT_MAX_CHARS - used)
        seen.update(i.id for g in recent_groups[: len(body)] for i in g)  # only what was shown counts as injected
        header = (
            f"Astrocyte memory is active for this project (bank `{bank}`). Relevant memories from "
            "earlier sessions are added to your context automatically; save durable decisions, "
            "conventions and preferences with the memory_retain tool."
        )
        if not body and not sections:
            return {"context": header + " No memories for this project yet."}
        if body:
            sections.append("Most recent memories:\n" + "\n".join(body))
        return {"context": header + "\n\n" + "\n\n".join(sections)}

    async def op_recall(self, req: dict) -> dict:
        bank, session, prompt = req["bank"], req.get("session_id") or "", req["prompt"]
        result = await self.brain.recall(prompt, bank_id=bank, max_results=8)
        hits = [h for h in result.hits if h.text]
        if not hits:
            return {"context": ""}
        vectors = await self.pipeline.llm_provider.embed([prompt] + [h.text for h in hits])
        scored = sorted(((_cosine(vectors[0], v), h) for v, h in zip(vectors[1:], hits)), key=lambda p: -p[0])
        best = scored[0][0]
        seen = self.injected.setdefault(session, set())
        lines = []
        for sim, hit in scored:
            if sim < INJECT_MIN_SIMILARITY or sim < best - INJECT_RELATIVE_WINDOW:
                break
            key = hit.memory_id or hit.text
            if key in seen:
                continue
            lines.append(render_memory(hit.text, hit.occurred_at or hit.retained_at))
            seen.add(key)
            if len(lines) == PROMPT_MAX_HITS:
                break
        lines = _budget(lines, PROMPT_MAX_CHARS)
        if not lines:
            return {"context": "", "best_similarity": round(best, 3)}
        text = "Possibly relevant memories from earlier sessions (Astrocyte):\n" + "\n".join(lines)
        return {"context": text, "best_similarity": round(best, 3)}

    async def op_capture(self, _: dict) -> dict:
        asyncio.get_running_loop().create_task(self.drain())
        return {"ok": True}

    # ── capture spool ─────────────────────────────────────────────────

    async def drain(self) -> int:
        """Retain every spooled turn. Progress is written back after each
        turn, so a crash mid-file resumes without duplicating memories."""
        stored = 0
        async with self._drain_lock:
            for path in sorted(spool_dir().glob("*.json")):
                try:
                    batch = json.loads(path.read_text(encoding="utf-8"))
                    turns = list(batch["turns"])
                    bank = batch["bank"]
                except (OSError, ValueError, KeyError, TypeError):
                    failed = spool_dir() / "failed"
                    failed.mkdir(exist_ok=True)
                    with contextlib.suppress(OSError):
                        path.rename(failed / path.name)
                    logger.warning("moved unreadable spool file %s aside", path.name)
                    continue
                while turns:
                    turn = turns[0]
                    started = turn.get("started_at")
                    result = await self.brain.retain(
                        turn["content"],
                        bank_id=bank,
                        content_type="conversation",
                        occurred_at=datetime.fromisoformat(started) if started else None,
                        metadata={"session_id": batch.get("session_id") or "", "source": batch.get("source") or "",
                                  # Files the turn read or edited, one per line (metadata is flat).
                                  **({"files": "\n".join(turn["files"])} if turn.get("files") else {})},
                        tags=["captured"],
                        source=batch.get("source") or "agent",
                    )
                    if not (getattr(result, "stored", False) or getattr(result, "deduplicated", False)):
                        logger.error("retain failed for %s: %s", path.name, getattr(result, "error", "?"))
                        return stored  # leave the rest for the next drain
                    stored += 1
                    turns.pop(0)
                    if turns:
                        _atomic_write(path, {**batch, "turns": turns})
                path.unlink(missing_ok=True)
        if stored:
            logger.info("captured %d turn(s)", stored)
        return stored

    # ── server ────────────────────────────────────────────────────────

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.last_activity = time.monotonic()
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            req = json.loads(line or b"{}")
            if self.token is not None and not hmac.compare_digest(str(req.pop("token", "")), self.token):
                logger.warning("refused a request without the daemon's token")
                writer.close()
                return
            op = getattr(self, f"op_{req.get('op', '')}", None)
            reply = await op(req) if op else {"error": f"unknown op {req.get('op')!r}"}
        except Exception as e:  # noqa: BLE001 — a bad request must not kill the daemon
            logger.exception("request failed")
            reply = {"error": f"{type(e).__name__}: {e}"}
        with contextlib.suppress(Exception):
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
            writer.close()

    async def housekeeping(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=DRAIN_INTERVAL_SECONDS)
            if self._stop.is_set():
                break
            try:
                await self.drain()
            except Exception:  # noqa: BLE001
                logger.exception("drain failed")
            try:
                changed = self.cfg_path.stat().st_mtime != self.cfg_mtime
            except OSError:
                changed = True
            if changed:
                logger.info("config changed; exiting so the next hook restarts on it")
                self._stop.set()
            elif time.monotonic() - self.last_activity > IDLE_EXIT_SECONDS:
                logger.info("idle; exiting")
                self._stop.set()

    async def serve(self, sock_path: Path) -> None:
        await self.warm()
        if transport() == "tcp":
            self.token = secrets.token_urlsafe(32)
            server = await asyncio.start_server(self.handle, host="127.0.0.1", port=0)
            port = server.sockets[0].getsockname()[1]
            endpoint = agentd_endpoint()
            _write_private(endpoint, {"transport": "tcp", "port": port, "token": self.token, "pid": os.getpid()})
            logger.info("serving on 127.0.0.1:%d (pid %d)", port, os.getpid())
            cleanup = endpoint
        else:
            with contextlib.suppress(FileNotFoundError):
                sock_path.unlink()  # stale: we hold the instance lock
            server = await asyncio.start_unix_server(self.handle, path=str(sock_path))
            os.chmod(sock_path, 0o600)
            logger.info("serving on %s (pid %d)", sock_path, os.getpid())
            cleanup = sock_path
        await self.drain()  # anything captured while no daemon was running
        async with server:
            await self.housekeeping()
        with contextlib.suppress(FileNotFoundError):
            cleanup.unlink()


def _replace(src: Path, dst: Path) -> None:
    """``os.replace``, retried briefly: on Windows it fails while another
    process (a hook reading state, a virus scanner) has ``dst`` open."""
    for attempt in range(4):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.05)


def _write_private(path: Path, data: dict) -> None:
    """Atomically, readable by the owner only (0600 on POSIX; on Windows the
    state directory's profile ACLs)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    _replace(tmp, path)


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    _replace(tmp, path)


def spool_capture(bank: str, session_id: str, source: str, turns: list[dict]) -> Path:
    """Durably queue turns for the daemon (atomic rename, 0600)."""
    d = spool_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Time first so the drain keeps order; the random tail because a coarse
    # clock (Windows: ~15 ms) gives two captures in one tick the same name,
    # and the second would replace the first.
    path = d / f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"bank": bank, "session_id": session_id, "source": source, "turns": turns}, fh)
    os.replace(tmp, path)
    return path


# ── client ───────────────────────────────────────────────────────────────


def request(op: str, payload: dict[str, Any] | None = None, *, timeout: float = 2.0) -> dict | None:
    """One request to the daemon; None if it isn't reachable in time."""
    if not supported():
        return None
    message: dict[str, Any] = {"op": op, **(payload or {})}
    try:
        if transport() == "tcp":
            endpoint = json.loads(agentd_endpoint().read_text(encoding="utf-8"))
            message["token"] = endpoint["token"]
            s = socket.create_connection(("127.0.0.1", int(endpoint["port"])), timeout=timeout)
        else:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(str(agentd_socket()))
        with s:
            s.sendall((json.dumps(message) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf) if buf else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def spawn(cfg: Path) -> None:
    """Start the daemon detached from the hook (and from the agent session),
    unless one was started moments ago."""
    if not supported():
        return
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = d / "agentd.spawned"
    with contextlib.suppress(OSError):
        if time.time() - marker.stat().st_mtime < 5:
            return
    marker.touch()
    # Not agentd.log: the daemon rotates that, and Windows can't rename a file
    # another handle holds open. This catches what precedes logging (a crash on import).
    log = open(d / "agentd.stdio.log", "ab")  # noqa: SIM115 — handed to the child process
    argv = [sys.executable, "-I", "-m", "astrocyte.harness.agentd", "--config", str(cfg)]
    try:
        if os.name == "nt":
            # No console window; out of the hook's process group; and out of the
            # agent's job object where allowed, or the daemon dies with the session.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                                 creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
            except OSError:  # the job forbids breakaway: live as long as the session
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                                 creationflags=flags)
        else:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                             close_fds=True)
    finally:
        log.close()


def ensure_running(cfg: Path, *, wait: float) -> bool:
    if request("ping", timeout=0.3):
        return True
    spawn(cfg)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.15)
        if request("ping", timeout=0.3):
            return True
    return False


# ── entry point ──────────────────────────────────────────────────────────


def _lock_exclusively(fh: Any) -> bool:
    """Take the single-instance lock without waiting; False if it's held."""
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def run(cfg: Path) -> int:
    if not supported():
        print("astrocyte agentd: no local transport on this platform.", file=sys.stderr)
        return 1
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.handlers.RotatingFileHandler(d / "agentd.log", maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    lock = open(d / "agentd.lock", "w")  # noqa: SIM115 — held for the process lifetime
    if not _lock_exclusively(lock):
        return 0  # another daemon owns the socket
    try:
        asyncio.run(AgentDaemon(cfg).serve(agentd_socket()))
    except Exception:  # noqa: BLE001
        logger.exception("agentd crashed")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="astrocyte agentd", description=__doc__.splitlines()[0])
    p.add_argument("--config", default=None)
    args = p.parse_args(argv)
    return run(Path(args.config).expanduser() if args.config else config_path())


if __name__ == "__main__":
    raise SystemExit(main())
