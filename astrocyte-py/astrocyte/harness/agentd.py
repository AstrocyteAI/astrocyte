"""``astrocyte agentd`` — warm memory for agent hooks.

Why a daemon: a hook is a fresh process per event. Measured on an Apple
Silicon laptop, the first recall in a fresh process costs ~1.1 s (mostly
importing fastembed), against 8 ms in a warm one — and ``UserPromptSubmit``
blocks the user's prompt until the hook returns. So hooks talk to this
per-user process over a Unix socket and never load a model themselves.

Lifecycle: started on demand by the hooks, one instance per user (flock),
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
import json
import logging
import logging.handlers
import math
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import agentd_socket, config_path, spool_dir, state_dir

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
DRAIN_INTERVAL_SECONDS = 20.0
IDLE_EXIT_SECONDS = float(os.environ.get("ASTROCYTE_AGENTD_IDLE", "1800"))

# The conversation pipeline stores turns with single newlines between roles,
# whatever separator capture wrote, so match any run of whitespace.
_TURN = re.compile(r"^\*\*user\*\*:\s*(?P<q>.*?)\s*\*\*assistant\*\*:\s*(?P<a>.*)$", re.DOTALL)


def supported() -> bool:
    return hasattr(socket, "AF_UNIX") and os.name != "nt"


# ── rendering ────────────────────────────────────────────────────────────


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_memory(text: str, when: datetime | None) -> str:
    """One line per memory. Captured turns become "Q … → A …"; the answer
    gets most of the room because that is where conclusions live."""
    m = _TURN.match(text.strip())
    if m:
        q = _clip(m["q"], 110)
        body = f"Q: {q} → A: {_clip(m['a'], ITEM_MAX_CHARS - len(q) - 12)}"
    else:
        body = _clip(text, ITEM_MAX_CHARS)
    return f"- [{when.date().isoformat()}] {body}" if when else f"- {body}"


def _budget(lines: list[str], limit: int) -> list[str]:
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > limit:
            break
        out.append(line)
        used += len(line) + 1
    return out


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

    async def op_boot(self, req: dict) -> dict:
        bank, session = req["bank"], req.get("session_id") or ""
        if req.get("source") in ("clear", "compact"):
            self.injected.pop(session, None)  # earlier injections left the context
        seen = self.injected.setdefault(session, set())
        lines: list[str] = []
        recent = getattr(self.pipeline.vector_store, "list_recent_vectors", None)
        if recent is not None:
            for item in await recent(bank, limit=BOOT_MAX_ITEMS):
                lines.append(render_memory(item.text, item.occurred_at or item.retained_at))
                seen.add(item.id)
        header = (
            f"Astrocyte memory is active for this project (bank `{bank}`). Relevant memories from "
            "earlier sessions are added to your context automatically; save durable decisions, "
            "conventions and preferences with the memory_retain tool."
        )
        if not lines:
            return {"context": header + " No memories for this project yet."}
        body = _budget(lines, BOOT_MAX_CHARS)
        return {"context": header + "\n\nMost recent memories:\n" + "\n".join(body)}

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
                        metadata={"session_id": batch.get("session_id") or "", "source": batch.get("source") or ""},
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
        with contextlib.suppress(FileNotFoundError):
            sock_path.unlink()  # stale: we hold the instance lock
        server = await asyncio.start_unix_server(self.handle, path=str(sock_path))
        os.chmod(sock_path, 0o600)
        logger.info("serving on %s (pid %d)", sock_path, os.getpid())
        await self.drain()  # anything captured while no daemon was running
        async with server:
            await self.housekeeping()
        with contextlib.suppress(FileNotFoundError):
            sock_path.unlink()


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def spool_capture(bank: str, session_id: str, source: str, turns: list[dict]) -> Path:
    """Durably queue turns for the daemon (atomic rename, 0600)."""
    d = spool_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = d / f"{time.time_ns()}-{os.getpid()}.json"
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
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(agentd_socket()))
            s.sendall((json.dumps({"op": op, **(payload or {})}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf) if buf else None
    except (OSError, ValueError):
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
    log = open(d / "agentd.log", "ab")  # noqa: SIM115 — handed to the child process
    subprocess.Popen(
        [sys.executable, "-I", "-m", "astrocyte.harness.agentd", "--config", str(cfg)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
    )
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


def run(cfg: Path) -> int:
    if not supported():
        print("astrocyte agentd needs Unix domain sockets (macOS or Linux).", file=sys.stderr)
        return 1
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.handlers.RotatingFileHandler(d / "agentd.log", maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    import fcntl

    lock = open(d / "agentd.lock", "w")  # noqa: SIM115 — held for the process lifetime
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
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
