"""Automatic memory: project banks, transcript capture, the agent daemon,
the hooks, and their installation into Claude Code.

Hermetic: temp HOME / XDG dirs, in-memory store and mock provider. Where a
test needs real semantics (similarity gating) it substitutes a controlled
embedder rather than trusting mock vectors.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from astrocyte.harness import agentd, hooks
from astrocyte.harness.hosts import ClaudeCodeHost, CodexHost, HostConfigError
from astrocyte.harness.project import project_bank
from astrocyte.harness.transcript import MAX_USER_CHARS, read_new_turns

MINIMAL = "vector_store: in_memory\nllm_provider: mock\nbarriers:\n  pii:\n    mode: disabled\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated HOME / config / state, with a valid config in place."""
    home = tmp_path / "home"
    home.mkdir()
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "ASTROCYTE_CONFIG", "ASTROCYTE_HOOKS", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    # Short state path: AF_UNIX socket paths are capped at ~104 bytes on macOS.
    state = Path("/tmp") / f"astro-test-{time.time_ns()}"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    cfg = home / ".config" / "astrocyte" / "astrocyte.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(MINIMAL)
    # Hooks consult the real process tree; pin it so results don't depend on
    # whether the test runner happens to sit under a `claude -p`.
    monkeypatch.setattr(hooks, "headless_session", lambda *_: False)
    yield Namespace(home=home, cfg=cfg, state=state / "astrocyte")
    subprocess.run(["rm", "-rf", str(state)], check=False)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


# ── project banks ────────────────────────────────────────────────────────


class TestProjectBank:
    def test_subdirectories_share_the_repo_bank(self, tmp_path):
        repo = tmp_path / "svc"
        (repo / "src" / "deep").mkdir(parents=True)
        git(repo, "init", "-q")
        assert project_bank(str(repo / "src" / "deep")) == project_bank(str(repo))

    def test_clones_and_worktrees_share_a_bank_through_the_remote(self, tmp_path):
        a, b = tmp_path / "clone-a", tmp_path / "elsewhere" / "clone-b"
        for d, url in ((a, "git@github.com:Org/Payments.git"), (b, "https://github.com/org/payments")):
            d.mkdir(parents=True)
            git(d, "init", "-q")
            git(d, "remote", "add", "origin", url)
        assert project_bank(str(a)) == project_bank(str(b))
        assert project_bank(str(a)).startswith("project:payments-")

    def test_unrelated_repos_with_the_same_name_do_not_collide(self, tmp_path):
        a, b = tmp_path / "one" / "api", tmp_path / "two" / "api"
        for d in (a, b):
            d.mkdir(parents=True)
            git(d, "init", "-q")
        assert project_bank(str(a)) != project_bank(str(b))

    def test_bank_ids_pass_astrocyte_validation(self, tmp_path):
        from astrocyte._validation import validate_bank_id

        weird = tmp_path / "My Project (v2) ✓"
        weird.mkdir()
        validate_bank_id(project_bank(str(weird)))


# ── transcript capture ───────────────────────────────────────────────────


def _line(**kw) -> str:
    return json.dumps(kw) + "\n"


def human(text: str, ts: str = "2026-10-01T10:00:00Z", **extra) -> str:
    return _line(type="user", message={"role": "user", "content": text}, origin={"kind": "human"},
                 timestamp=ts, **extra)


def assistant(*blocks: dict, sidechain: bool = False) -> str:
    return _line(type="assistant", message={"role": "assistant", "content": list(blocks)}, isSidechain=sidechain)


def text(t: str) -> dict:
    return {"type": "text", "text": t}


class TestTranscriptEdges:
    """Whatever the transcript holds, the reader yields only real turns and
    never loses its place."""

    def _read(self, tmp_path, body: str, offset: int = 0):
        t = tmp_path / "t.jsonl"
        t.write_text(body)
        return read_new_turns(t, offset)

    def test_prompts_typed_as_text_blocks_count_tool_results_do_not(self, tmp_path):
        blocks = _line(type="user", message={"content": [text("block prompt")]}, timestamp="2026-10-01T10:00:00Z")
        tool = _line(type="user", message={"content": [{"type": "tool_result", "content": "ls output"}]})
        turns, _ = self._read(tmp_path, blocks + assistant(text("answer")) + tool + assistant(text("more")))
        assert [(t.user, t.assistant) for t in turns] == [("block prompt", ["answer", "more"])]

    @pytest.mark.parametrize("line", [
        _line(type="user", message={"content": 42}),  # not text at all
        _line(type="user", message={"content": "from a hook"}, origin={"kind": "hook"}),  # not the human
        _line(type="user", message={"content": "<command-name>/clear</command-name>"}),  # a slash-command wrapper
        _line(type="user", message={"content": "<system-reminder>only this</system-reminder>"}),  # empty once stripped
        _line(type="user", message={"content": "side"}, isSidechain=True),  # a subagent
        "[1, 2, 3]\n",  # JSON, but not an object
        "{broken json\n",
    ])
    def test_lines_that_are_not_a_human_prompt_are_skipped(self, tmp_path, line):
        turns, _ = self._read(tmp_path, line + human("real") + assistant(text("answer")))
        assert [t.user for t in turns] == ["real"]

    @pytest.mark.parametrize("content,expected", [("plain string answer", ["plain string answer"]), ("   ", []),
                                                  (None, [])])
    def test_assistant_content_forms(self, tmp_path, content, expected):
        reply = _line(type="assistant", message={"content": content})
        turns, _ = self._read(tmp_path, human("q") + reply + assistant(text("tail")))
        assert turns[0].assistant == [*expected, "tail"]

    @pytest.mark.parametrize("ts", [None, 1700000000, "yesterday"])
    def test_unusable_timestamps_are_none(self, tmp_path, ts):
        turns, _ = self._read(tmp_path, human("q", ts=ts) + assistant(text("a")))
        assert turns[0].started_at is None

    def test_a_missing_transcript_reads_nothing_and_keeps_the_offset(self, tmp_path):
        assert read_new_turns(tmp_path / "gone.jsonl", 123) == ([], 123)

    def test_a_truncated_transcript_is_read_from_the_start(self, tmp_path):
        """Offsets beyond the end mean the file was rewritten (e.g. /clear)."""
        turns, resume = self._read(tmp_path, human("q") + assistant(text("a")), offset=10_000)
        assert [t.user for t in turns] == ["q"] and resume > 0

    def test_a_prompt_still_awaiting_its_answer_is_re_read_next_time(self, tmp_path):
        body = human("first") + assistant(text("one")) + human("second")
        turns, resume = self._read(tmp_path, body)
        assert [t.user for t in turns] == ["first"]
        later = body + assistant(text("two"))
        (tmp_path / "t.jsonl").write_text(later)
        turns, _ = read_new_turns(tmp_path / "t.jsonl", resume)
        assert [(t.user, t.assistant) for t in turns] == [("second", ["two"])]

    def test_garbage_between_turns_does_not_hold_back_the_offset(self, tmp_path):
        body = human("q") + assistant(text("a")) + "{broken\n"
        _, resume = self._read(tmp_path, body)
        assert resume == len(body.encode())


class TestTranscript:
    def test_keeps_human_prompts_and_assistant_prose_only(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text(
            human("Why is staging failing?<system-reminder>injected</system-reminder>")
            + assistant({"type": "thinking", "thinking": "secret reasoning"}, text("Migration 42 lacks a default."))
            + assistant({"type": "tool_use", "name": "Bash", "input": {"command": "cat .env"}})
            + _line(type="user", message={"role": "user", "content": [{"type": "tool_result", "content": "API_KEY=x"}]})
            + assistant(text("Fixed by backfilling first."))
            + assistant(text("subagent chatter"), sidechain=True)
            + _line(type="attachment", attachment={"type": "hook_success", "content": "Possibly relevant memories"})
            + _line(type="user", message={"content": "<task-notification>done</task-notification>"},
                    origin={"kind": "task-notification"})
        )
        turns, offset = read_new_turns(t)
        assert len(turns) == 1
        rendered = turns[0].render()
        assert "Why is staging failing?" in rendered and "injected" not in rendered
        assert "Migration 42 lacks a default." in rendered and "Fixed by backfilling first." in rendered
        for leaked in ("secret reasoning", "API_KEY", "cat .env", "subagent chatter", "Possibly relevant", "done"):
            assert leaked not in rendered
        assert offset == t.stat().st_size

    def test_incremental_reads_never_duplicate_or_lose_a_turn(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text(human("first question") + assistant(text("first answer")))
        turns, off = read_new_turns(t)
        assert [x.user for x in turns] == ["first question"]
        with t.open("a") as fh:
            fh.write(human("second question"))  # answer not written yet
        turns, off2 = read_new_turns(t, off)
        assert turns == [] and off2 == off, "an unanswered prompt must be re-read later"
        with t.open("a") as fh:
            fh.write(assistant(text("second answer")))
        turns, off3 = read_new_turns(t, off2)
        assert [x.user for x in turns] == ["second question"]
        assert read_new_turns(t, off3)[0] == []

    def test_half_written_last_line_is_left_for_next_time(self, tmp_path):
        t = tmp_path / "t.jsonl"
        full = human("q") + assistant(text("a"))
        t.write_text(full + '{"type": "user", "mess')
        turns, off = read_new_turns(t)
        assert len(turns) == 1 and off == len(full.encode())

    def test_long_messages_are_clipped(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text(human("x" * 50_000) + assistant(text("ok")))
        [turn] = read_new_turns(t)[0]
        assert len(turn.render()) < MAX_USER_CHARS + 200

    def test_a_rewritten_transcript_restarts_from_the_top(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text(human("q") + assistant(text("a")))
        assert len(read_new_turns(t, offset=10_000)[0]) == 1


# ── prompt gating ────────────────────────────────────────────────────────


@pytest.mark.parametrize("prompt", ["thanks!", "continue", "ok go ahead", "/compact", "yes please", ""])
def test_small_talk_never_reaches_the_daemon(prompt):
    assert not hooks.worth_recalling(prompt)


@pytest.mark.parametrize("prompt", ["why is the staging migration failing?", "fix test_payment_retry", "use httpx here"])
def test_real_questions_do(prompt):
    assert hooks.worth_recalling(prompt)


# ── daemon ───────────────────────────────────────────────────────────────


class ControlledEmbedder:
    """Maps each text to a fixed direction so similarities are known exactly."""

    def __init__(self, table: dict[str, list[float]]):
        self.table = table

    async def embed(self, texts):
        return [self.table.get(t, [0.0, 0.0, 1.0]) for t in texts]


def _hit(text: str, mid: str):
    from astrocyte.types import MemoryHit

    return MemoryHit(text=text, score=0.1, memory_id=mid)


@pytest.fixture
def daemon(env):
    return agentd.AgentDaemon(env.cfg)


class TestDaemonRecall:
    def _wire(self, daemon, monkeypatch, hits, table):
        from astrocyte.types import RecallResult

        async def fake_recall(query, **kw):
            return RecallResult(hits=hits, total_available=len(hits), truncated=False)

        monkeypatch.setattr(daemon.brain, "recall", fake_recall)
        monkeypatch.setattr(daemon.pipeline, "llm_provider", ControlledEmbedder(table))

    def test_unrelated_memories_are_never_injected(self, daemon, monkeypatch):
        """recall's fused score ranked an unrelated memory first in testing;
        only calibrated similarity may decide."""
        self._wire(daemon, monkeypatch, [_hit("billing is written in Go", "m1")],
                   {"weather in paris?": [1, 0, 0], "billing is written in Go": [0, 1, 0]})
        reply = asyncio.run(daemon.op_recall({"bank": "b", "session_id": "s", "prompt": "weather in paris?"}))
        assert reply["context"] == ""

    def test_relevant_memory_is_injected_once_per_session(self, daemon, monkeypatch):
        self._wire(daemon, monkeypatch, [_hit("we use httpx", "m1")],
                   {"which http client?": [1, 0, 0], "we use httpx": [0.9, 0.1, 0]})
        req = {"bank": "b", "session_id": "s", "prompt": "which http client?"}
        first = asyncio.run(daemon.op_recall(req))
        assert "we use httpx" in first["context"]
        assert asyncio.run(daemon.op_recall(req))["context"] == "", "already in context this session"
        other = asyncio.run(daemon.op_recall({**req, "session_id": "s2"}))
        assert "we use httpx" in other["context"]

    def test_weaker_neighbours_of_the_best_hit_are_dropped(self, daemon, monkeypatch):
        self._wire(daemon, monkeypatch, [_hit("strong", "a"), _hit("neighbour", "b")],
                   {"q about x": [1, 0, 0], "strong": [0.95, 0.05, 0], "neighbour": [0.7, 0.71, 0]})
        ctx = asyncio.run(daemon.op_recall({"bank": "b", "session_id": "s", "prompt": "q about x"}))["context"]
        assert "strong" in ctx and "neighbour" not in ctx

    def test_compaction_allows_reinjection(self, daemon, monkeypatch):
        self._wire(daemon, monkeypatch, [_hit("we use httpx", "m1")],
                   {"which http client?": [1, 0, 0], "we use httpx": [1, 0, 0]})
        req = {"bank": "b", "session_id": "s", "prompt": "which http client?"}
        asyncio.run(daemon.op_recall(req))
        asyncio.run(daemon.op_boot({"bank": "b", "session_id": "s", "source": "compact"}))
        assert "we use httpx" in asyncio.run(daemon.op_recall(req))["context"]

    @pytest.mark.parametrize("sep", ["\n\n", "\n", " "])
    def test_captured_turns_render_as_question_and_answer(self, sep):
        """The pipeline stores turns with single newlines (observed), whatever
        separator capture used."""
        line = agentd.render_memory(f"**user**: why?{sep}**assistant**: because migrations need defaults", None)
        assert line == "- Q: why? → A: because migrations need defaults"


class TestSpool:
    def test_drain_retains_every_spooled_turn_then_clears_the_spool(self, env, daemon):
        agentd.spool_capture("proj", "s1", "claude-code", [
            {"content": "**user**: q1\n\n**assistant**: we deploy on Tuesdays", "started_at": "2026-10-01T10:00:00+00:00"},
            {"content": "**user**: q2\n\n**assistant**: staging is on Fly.io", "started_at": None},
        ])
        assert asyncio.run(daemon.drain()) == 2
        assert list((env.state / "spool").glob("*.json")) == []
        hits = asyncio.run(daemon.brain.recall("staging", bank_id="proj")).hits
        assert any("Fly.io" in h.text for h in hits)

    def test_a_failed_retain_keeps_the_remaining_turns(self, env, daemon, monkeypatch):
        path = agentd.spool_capture("proj", "s1", "claude-code", [
            {"content": "turn one", "started_at": None}, {"content": "turn two", "started_at": None}])
        calls = []

        async def flaky(content, **kw):
            from astrocyte.types import RetainResult

            calls.append(content)
            return RetainResult(stored=len(calls) == 1, memory_id="x" if len(calls) == 1 else None)

        monkeypatch.setattr(daemon.brain, "retain", flaky)
        assert asyncio.run(daemon.drain()) == 1
        assert [t["content"] for t in json.loads(path.read_text())["turns"]] == ["turn two"]

    def test_unreadable_spool_files_are_moved_aside(self, env, daemon):
        (env.state / "spool").mkdir(parents=True)
        (env.state / "spool" / "1-1.json").write_text("{not json")
        asyncio.run(daemon.drain())
        assert (env.state / "spool" / "failed" / "1-1.json").exists()


class TestDaemonOverSocket:
    def test_ping_capture_recall_round_trip(self, env, monkeypatch):
        monkeypatch.setattr(agentd, "DRAIN_INTERVAL_SECONDS", 0.2)
        d = agentd.AgentDaemon(env.cfg)
        sock = agentd.agentd_socket()
        sock.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=lambda: loop.run_until_complete(d.serve(sock)), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while agentd.request("ping", timeout=0.3) is None:
                assert time.monotonic() < deadline, "daemon did not come up"
                time.sleep(0.1)
            agentd.spool_capture("proj", "s1", "claude-code", [{"content": "We freeze deploys on Fridays.", "started_at": None}])
            assert agentd.request("capture", timeout=2) == {"ok": True}
            deadline = time.monotonic() + 10
            while list(agentd.spool_dir().glob("*.json")):
                assert time.monotonic() < deadline, "spool was not drained"
                time.sleep(0.1)
            reply = agentd.request("boot", {"bank": "proj", "session_id": "s9"}, timeout=5)
            assert "bank `proj`" in reply["context"]
            assert agentd.request("bogus", timeout=2)["error"].startswith("unknown op")
        finally:
            loop.call_soon_threadsafe(d._stop.set)
            thread.join(timeout=10)
        assert not sock.exists(), "socket is removed on exit"

    def test_unreachable_daemon_is_none_not_an_exception(self, env):
        assert agentd.request("ping", timeout=0.2) is None


def _sqlite_daemon(env, tmp_path):
    pytest.importorskip("astrocyte_sqlite")
    env.cfg.write_text(MINIMAL.replace("vector_store: in_memory", "vector_store: sqlite")
                       + f"vector_store_config:\n  path: {tmp_path / 'm.db'}\n")
    return agentd.AgentDaemon(env.cfg)


def _turn(q: str, a: str, at: str) -> dict:
    return {"content": f"**user**: {q}\n\n**assistant**: {a}", "started_at": at}


class TestDaemonOps:
    def test_boot_lists_the_projects_recent_memories(self, env, tmp_path):
        """The session summary needs a store that can list by recency (SQLite,
        the local default); its memories then count as already injected."""
        d = _sqlite_daemon(env, tmp_path)
        asyncio.run(d.brain.retain("Deploys go out on Tuesdays.", bank_id="proj",
                                   occurred_at=datetime(2026, 10, 1, 10, tzinfo=timezone.utc)))
        reply = asyncio.run(d.op_boot({"bank": "proj", "session_id": "s2"}))
        assert "Most recent memories:" in reply["context"] and "Deploys go out on Tuesdays." in reply["context"]
        assert "[2026-10-01]" in reply["context"], "dated by when it was said"
        assert "Where you left off" not in reply["context"], "no captured session yet"
        assert d.injected["s2"], "boot memories are not injected again by prompt recall"

    def test_boot_opens_with_where_the_previous_session_left_off(self, env, tmp_path):
        d = _sqlite_daemon(env, tmp_path)
        agentd.spool_capture("proj", "s1", "codex", [
            _turn("which queue?", "SQS, decided last week.", "2026-10-01T10:00:00+00:00"),
            _turn("deploy day?", "Tuesdays.", "2026-10-01T10:05:00+00:00"),
            _turn("what's left?", "Only the retry test; then tag the release.", "2026-10-01T10:09:00+00:00"),
        ])
        agentd.spool_capture("proj", "s0", "claude-code", [
            _turn("older session?", "Yes, this one is older.", "2026-09-28T09:00:00+00:00")])
        asyncio.run(d.drain())
        ctx = asyncio.run(d.op_boot({"bank": "proj", "session_id": "s2"}))["context"]
        resume, _, rest = ctx.partition("Most recent memories:")
        assert "Where you left off: the previous session here (Codex, " in resume
        # Its closing turns, oldest first, ending with the last answer.
        assert resume.index("which queue?") < resume.index("deploy day?") < resume.index("what's left?")
        assert resume.rstrip().endswith("A: Only the retry test; then tag the release.")
        assert "older session?" not in resume, "only the most recent other session"
        assert "what's left?" not in rest, "not shown twice"
        assert len(d.injected["s2"]) >= 3, "resumed turns count as already injected"

    def test_resume_skips_the_current_session_and_interleaved_ones(self, env, tmp_path):
        """Two sessions running side by side interleave in the store; and a
        session that restarts (compact, clear) must not be told about itself."""
        d = _sqlite_daemon(env, tmp_path)
        agentd.spool_capture("proj", "a", "claude-code", [_turn("a1?", "first of a.", "2026-10-01T10:00:00+00:00")])
        agentd.spool_capture("proj", "b", "codex", [_turn("b1?", "first of b.", "2026-10-01T10:01:00+00:00")])
        agentd.spool_capture("proj", "a", "claude-code", [_turn("a2?", "last of a.", "2026-10-01T10:02:00+00:00")])
        agentd.spool_capture("proj", "c", "codex", [_turn("c1?", "current session.", "2026-10-01T10:03:00+00:00")])
        asyncio.run(d.drain())
        resume = asyncio.run(d.op_boot({"bank": "proj", "session_id": "c"}))["context"].partition(
            "Most recent memories:")[0]
        assert "(Claude Code, " in resume and "a1?" in resume and "a2?" in resume
        assert "b1?" not in resume and "c1?" not in resume

    def test_resume_gives_the_last_answer_the_most_room(self, env, tmp_path):
        d = _sqlite_daemon(env, tmp_path)
        agentd.spool_capture("proj", "s1", "codex", [
            _turn("earlier?", " ".join(f"alpha{i}" for i in range(150)), "2026-10-01T10:00:00+00:00"),
            _turn("final?", " ".join(f"omega{i}" for i in range(150)), "2026-10-01T10:01:00+00:00")])
        asyncio.run(d.drain())
        ctx = asyncio.run(d.op_boot({"bank": "proj", "session_id": "s2"}))["context"]
        lines = [ln for ln in ctx.splitlines() if ln.startswith("- ")]
        # A long turn is stored as several overlapping chunks; each turn is
        # still one line, ending exactly where its answer ended.
        assert len(lines) == 2, ctx
        assert lines[0].startswith("- Q: earlier? → A: …") and lines[0].endswith("alpha149")
        assert lines[1].startswith("- Q: final? → A: …") and lines[1].endswith("omega149")
        words = lines[1].split("…", 1)[1].split()
        assert words == [f"omega{i}" for i in range(150 - len(words), 150)], "no gaps or repeats at chunk seams"
        assert len(lines[1]) > len(lines[0]) + 200
        assert len(lines[1]) <= agentd.RESUME_LAST_MAX_CHARS + 10
        assert "Most recent memories:" not in ctx, "both turns are already shown"

    def test_chunked_memories_are_one_line_in_the_recent_list_too(self, env, tmp_path):
        d = _sqlite_daemon(env, tmp_path)
        agentd.spool_capture("proj", "s1", "codex", [
            _turn("long one?", " ".join(f"beta{i}" for i in range(150)), "2026-10-01T10:00:00+00:00")])
        asyncio.run(d.drain())
        ctx = asyncio.run(d.op_boot({"bank": "proj", "session_id": "s1"}))["context"]  # its own session: no resume
        lines = [ln for ln in ctx.splitlines() if ln.startswith("- ")]
        assert len(lines) == 1 and lines[0].endswith("beta149"), ctx
        assert len(d.injected["s1"]) > 1, "every chunk of a shown turn counts as injected"

    def test_join_overlapping(self):
        assert agentd._join_overlapping("the quick brown fox jumps high", "brown fox jumps high over the dog") == (
            "the quick brown fox jumps high over the dog")
        # A short coincidence is not an overlap: chunk overlaps are long.
        assert agentd._join_overlapping("ends with the", "the start") == "ends with the the start"
        assert agentd._join_overlapping("", "start") == "start"
        assert agentd._join_overlapping("no shared text here", "completely different") == (
            "no shared text here completely different")

    @pytest.mark.parametrize("minutes, expected", [
        (0, "just now"), (5, "5 minutes ago"), (60, "1 hour ago"), (150, "2 hours ago"), (3 * 24 * 60, "3 days ago")])
    def test_ago(self, minutes, expected):
        now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
        assert agentd._ago(now - timedelta(minutes=minutes), now) == expected
        assert agentd._ago((now - timedelta(minutes=minutes)).replace(tzinfo=None), now) == expected

    def test_boot_on_an_empty_bank_says_so(self, daemon):
        assert "No memories for this project yet" in asyncio.run(daemon.op_boot({"bank": "empty"}))["context"]

    def test_recall_with_no_hits_injects_nothing(self, daemon):
        assert asyncio.run(daemon.op_recall({"bank": "empty", "prompt": "why is staging failing"})) == {"context": ""}

    def test_a_malformed_request_gets_an_error_and_the_daemon_lives(self, env, daemon):
        class Reader:
            async def readline(self):
                return b"{not json\n"

        class Writer:
            data = b""

            def write(self, b):
                Writer.data += b

            async def drain(self):
                pass

            def close(self):
                pass

        asyncio.run(daemon.handle(Reader(), Writer()))
        assert json.loads(Writer.data)["error"].startswith("JSONDecodeError")


class TestDaemonHousekeeping:
    """The daemon must not outlive its purpose: a config change (setup or a
    hand edit) or 30 idle minutes ends it; the next hook starts a fresh one."""

    def _run(self, d, timeout=5):
        asyncio.run(asyncio.wait_for(d.housekeeping(), timeout=timeout))

    def test_exits_when_the_config_changes(self, env, monkeypatch, daemon):
        monkeypatch.setattr(agentd, "DRAIN_INTERVAL_SECONDS", 0.05)
        daemon.cfg_mtime -= 10  # as if the file was edited after start
        self._run(daemon)
        assert daemon._stop.is_set()

    def test_exits_when_the_config_is_deleted(self, env, monkeypatch, daemon):
        monkeypatch.setattr(agentd, "DRAIN_INTERVAL_SECONDS", 0.05)
        env.cfg.unlink()
        self._run(daemon)
        assert daemon._stop.is_set()

    def test_exits_when_idle_and_survives_a_failing_drain(self, env, monkeypatch, daemon):
        monkeypatch.setattr(agentd, "DRAIN_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agentd, "IDLE_EXIT_SECONDS", 0.0)

        async def broken():
            raise RuntimeError("disk gone")

        monkeypatch.setattr(daemon, "drain", broken)
        self._run(daemon)
        assert daemon._stop.is_set()


class TestDaemonProcess:
    """The real thing: hooks start the daemon as a detached process."""

    def test_spawned_on_demand_single_instance_and_debounced(self, env):
        assert agentd.ensure_running(env.cfg, wait=30), (env.state / "agentd.log").read_text()
        pid = agentd.request("ping")["pid"]
        try:
            assert pid != os.getpid()
            # A second daemon finds the lock held and leaves at once.
            second = subprocess.run([sys.executable, "-I", "-m", "astrocyte.harness.agentd", "--config",
                                     str(env.cfg)], capture_output=True, text=True, timeout=30)
            assert second.returncode == 0 and agentd.request("ping")["pid"] == pid
            # A burst of prompts while it starts must not fork a daemon each.
            marker = env.state / "agentd.spawned"
            before = marker.stat().st_mtime
            agentd.spawn(env.cfg)
            assert marker.stat().st_mtime == before
            assert agentd.ensure_running(env.cfg, wait=1), "already running: no wait"
        finally:
            os.kill(pid, 15)

    def test_ensure_running_gives_up_by_the_deadline(self, env, monkeypatch):
        monkeypatch.setattr(agentd, "spawn", lambda cfg: None)  # a daemon that never comes up
        started = time.monotonic()
        assert agentd.ensure_running(env.cfg, wait=0.5) is False
        assert time.monotonic() - started < 3


class TestWithoutUnixSockets:
    """Windows: no AF_UNIX daemon. Hooks degrade to "no memory", never errors."""

    def test_client_and_launcher_are_inert(self, env, monkeypatch, capsys):
        monkeypatch.setattr(agentd, "supported", lambda: False)
        assert agentd.request("ping") is None
        with monkeypatch.context() as m:  # scoped: fixture teardown needs the real Popen
            m.setattr(agentd.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned on an unsupported OS"))
            agentd.spawn(env.cfg)
        assert agentd.run(env.cfg) == 1 and "Unix domain sockets" in capsys.readouterr().err


# ── hooks ────────────────────────────────────────────────────────────────


class TestHooks:
    def test_silent_when_not_set_up(self, env, capsys, monkeypatch):
        env.cfg.unlink()
        monkeypatch.setattr(agentd, "ensure_running", lambda *a, **k: pytest.fail("must not start a daemon"))
        assert hooks.run("session-start", json.dumps({"cwd": str(env.home)})) == 0
        assert capsys.readouterr().out == ""

    def test_paused_by_env(self, env, capsys, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_HOOKS", "off")
        monkeypatch.setattr(agentd, "request", lambda *a, **k: pytest.fail("paused"))
        hooks.run("prompt", json.dumps({"prompt": "why is staging failing", "cwd": str(env.home)}))
        assert capsys.readouterr().out == ""

    def test_garbage_input_never_raises(self, env, capsys):
        for stdin in ("", "not json", "[1,2]", json.dumps({"cwd": 42})):
            assert hooks.run("prompt", stdin) == 0
        assert capsys.readouterr().out == ""

    def test_trivial_prompt_skips_the_daemon(self, env, monkeypatch):
        monkeypatch.setattr(agentd, "request", lambda *a, **k: pytest.fail("trivial prompt reached the daemon"))
        hooks.run("prompt", json.dumps({"prompt": "thanks!", "cwd": str(env.home)}))

    def test_daemon_down_means_no_context_and_a_warm_up(self, env, capsys, monkeypatch):
        spawned = []
        monkeypatch.setattr(agentd, "request", lambda *a, **k: None)
        monkeypatch.setattr(agentd, "spawn", lambda cfg: spawned.append(cfg))
        hooks.run("prompt", json.dumps({"prompt": "why is staging failing", "cwd": str(env.home)}))
        assert capsys.readouterr().out == "" and spawned == [env.cfg]

    def test_prompt_context_uses_the_hook_json_contract(self, env, capsys, monkeypatch):
        seen = {}

        def fake_request(op, payload=None, **kw):
            seen.update(op=op, **(payload or {}))
            return {"context": "Possibly relevant memories:\n- x"}

        monkeypatch.setattr(agentd, "request", fake_request)
        hooks.run("prompt", json.dumps({"prompt": "why is staging failing", "cwd": str(env.home), "session_id": "s1"}))
        out = json.loads(capsys.readouterr().out)
        assert out == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                              "additionalContext": "Possibly relevant memories:\n- x"}}
        assert seen["op"] == "recall" and seen["bank"] == project_bank(str(env.home)) and seen["session_id"] == "s1"

    def test_stop_spools_new_turns_once(self, env, tmp_path, monkeypatch):
        monkeypatch.setattr(agentd, "request", lambda *a, **k: None)
        monkeypatch.setattr(agentd, "spawn", lambda cfg: None)
        t = tmp_path / "t.jsonl"
        t.write_text(human("what's our deploy day?") + assistant(text("Tuesdays.")))
        payload = json.dumps({"transcript_path": str(t), "session_id": "s1", "cwd": str(env.home)})
        hooks.run("stop", payload)
        hooks.run("stop", payload)  # nothing new
        files = list((env.state / "spool").glob("*.json"))
        assert len(files) == 1
        batch = json.loads(files[0].read_text())
        assert batch["bank"] == project_bank(str(env.home)) and "Tuesdays." in batch["turns"][0]["content"]
        assert oct(files[0].stat().st_mode & 0o777) == "0o600", "captured conversations are private"



class TestSessionStartHook:
    def test_boots_the_daemon_and_injects_the_project_summary(self, env, capsys, monkeypatch):
        seen = {}
        monkeypatch.setattr(agentd, "ensure_running", lambda cfg, wait: seen.setdefault("wait", wait) and True)

        def fake_request(op, payload=None, **kw):
            seen.update(op=op, **(payload or {}))
            return {"context": "Astrocyte memory is active"}

        monkeypatch.setattr(agentd, "request", fake_request)
        hooks.run("session-start", json.dumps({"cwd": str(env.home), "session_id": "s1", "source": "resume"}))
        assert json.loads(capsys.readouterr().out) == {"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": "Astrocyte memory is active"}}
        assert seen["op"] == "boot" and seen["source"] == "resume" and seen["bank"] == project_bank(str(env.home))
        assert seen["wait"] == hooks.SESSION_START_WAIT

    def test_a_daemon_that_will_not_start_means_no_summary(self, env, capsys, monkeypatch):
        monkeypatch.setattr(agentd, "ensure_running", lambda cfg, wait: False)
        monkeypatch.setattr(agentd, "request", lambda *a, **k: pytest.fail("no daemon to ask"))
        hooks.run("session-start", json.dumps({"cwd": str(env.home)}))
        assert capsys.readouterr().out == ""

    def test_a_crashing_handler_is_logged_and_the_agent_carries_on(self, env, capsys, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("socket on fire")

        monkeypatch.setattr(agentd, "ensure_running", boom)
        assert hooks.run("session-start", json.dumps({"cwd": str(env.home)})) == 0
        assert capsys.readouterr().out == ""
        assert "socket on fire" in (env.state / "hooks.log").read_text()

    def test_main_reads_the_payload_from_stdin(self, env, monkeypatch):
        import io

        got = []
        monkeypatch.setattr(hooks, "run", lambda event, text, host: got.append((event, text, host)) or 0)
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"prompt": "x"}'))
        assert hooks.main("prompt", "codex") == 0
        assert got == [("prompt", '{"prompt": "x"}', "codex")]


class TestAncestry:
    """Headless detection walks the process tree; every failure mode must
    read as "not found" (interactive), never raise inside a hook."""

    def _ps(self, monkeypatch, table):
        def fake_run(argv, **kw):
            pid = int(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout=table.get(pid, ""), stderr="")

        monkeypatch.setattr(hooks.subprocess, "run", fake_run)
        monkeypatch.setattr(hooks.os, "getppid", lambda: 100)

    def test_finds_the_agent_through_a_shell(self, monkeypatch):
        self._ps(monkeypatch, {100: "50 /bin/zsh -c hook", 50: "1 /opt/homebrew/bin/codex exec hi"})
        assert hooks._agent_ancestor_args("codex") == ["/opt/homebrew/bin/codex", "exec", "hi"]

    @pytest.mark.parametrize("table", [
        {100: "1 /bin/zsh"},  # reached init without finding it
        {},  # ps printed nothing (process gone)
        {100: "x /bin/zsh"},  # unparseable parent pid
    ])
    def test_not_found_is_none(self, monkeypatch, table):
        self._ps(monkeypatch, table)
        assert hooks._agent_ancestor_args("codex") is None

    def test_ps_failing_is_none(self, monkeypatch):
        def broken(*a, **k):
            raise OSError("no ps")

        monkeypatch.setattr(hooks.subprocess, "run", broken)
        assert hooks._agent_ancestor_args("claude") is None

    def test_depth_is_bounded(self, monkeypatch):
        self._ps(monkeypatch, {pid: f"{pid + 1} /bin/sh" for pid in range(100, 200)})
        assert hooks._agent_ancestor_args("claude", max_depth=3) is None


class TestCodexHooks:
    """Codex fires the same events with the same contract (verified live on
    codex-cli 0.160: both context injections reached the model), but its Stop
    carries the reply instead of a transcript we can read."""

    def _capture(self, monkeypatch):
        monkeypatch.setattr(agentd, "request", lambda *a, **k: None)
        monkeypatch.setattr(agentd, "spawn", lambda cfg: None)

    def _spooled(self, env) -> list[dict]:
        return [json.loads(f.read_text()) for f in sorted((env.state / "spool").glob("*.json"))]

    def test_a_turn_is_the_kept_prompt_plus_the_reply(self, env, monkeypatch, capsys):
        self._capture(monkeypatch)
        base = {"session_id": "c1", "cwd": str(env.home), "turn_id": "t1"}
        hooks.run("prompt", json.dumps({**base, "prompt": "what's our deploy day?"}), "codex")
        hooks.run("stop", json.dumps({**base, "last_assistant_message": "Tuesdays.", "stop_hook_active": False}),
                  "codex")
        [batch] = self._spooled(env)
        assert batch["source"] == "codex" and batch["bank"] == project_bank(str(env.home))
        content = batch["turns"][0]["content"]
        assert "what's our deploy day?" in content and "Tuesdays." in content
        # Codex fails a Stop hook that emits additionalContext (observed).
        assert capsys.readouterr().out == ""

    def test_each_turn_is_captured_once(self, env, monkeypatch):
        self._capture(monkeypatch)
        base = {"session_id": "c1", "cwd": str(env.home)}
        hooks.run("prompt", json.dumps({**base, "prompt": "first question here"}), "codex")
        hooks.run("stop", json.dumps({**base, "last_assistant_message": "one"}), "codex")
        hooks.run("stop", json.dumps({**base, "last_assistant_message": "one"}), "codex")
        assert len(self._spooled(env)) == 1

    def test_a_turn_begun_before_the_hooks_is_skipped(self, env, monkeypatch):
        """No kept prompt means half a turn; half a turn is not a memory."""
        self._capture(monkeypatch)
        hooks.run("stop", json.dumps({"session_id": "c1", "cwd": str(env.home), "last_assistant_message": "x"}),
                  "codex")
        assert not (env.state / "spool").exists()

    def test_slight_prompts_skip_recall_but_still_make_a_turn(self, env, monkeypatch):
        calls = []
        monkeypatch.setattr(agentd, "request", lambda op, *a, **k: calls.append(op) or None)
        monkeypatch.setattr(agentd, "spawn", lambda cfg: None)
        base = {"session_id": "c1", "cwd": str(env.home)}
        hooks.run("prompt", json.dumps({**base, "prompt": "yes"}), "codex")
        assert "recall" not in calls
        hooks.run("stop", json.dumps({**base, "last_assistant_message": "Deployed to staging."}), "codex")
        assert len(self._spooled(env)) == 1

    def test_injection_uses_the_shared_contract(self, env, monkeypatch, capsys):
        monkeypatch.setattr(agentd, "request", lambda *a, **k: {"context": "Possibly relevant memories:\n- x"})
        hooks.run("prompt", json.dumps({"prompt": "why is staging failing", "cwd": str(env.home),
                                        "session_id": "c1"}), "codex")
        assert json.loads(capsys.readouterr().out) == {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": "Possibly relevant memories:\n- x"}}

    def test_credentials_never_reach_the_spool(self, env, monkeypatch):
        """The spool is plaintext on disk and is replayed into other agents'
        prompts later; a pasted key must be gone before it is written."""
        self._capture(monkeypatch)
        key = "sk-" + "proj-" + "Ab3" * 15
        base = {"session_id": "c1", "cwd": str(env.home)}
        hooks.run("prompt", json.dumps({**base, "prompt": f"why does {key} get a 401?"}), "codex")
        assert not any(key in f.read_text() for f in (env.state / "sessions").glob("*")), "kept prompt is scrubbed"
        hooks.run("stop", json.dumps({**base, "last_assistant_message": f"The key {key} was revoked."}), "codex")
        raw = next((env.state / "spool").glob("*.json")).read_text()
        assert key not in raw and raw.count("[SECRET_REDACTED]") == 2

    def test_an_empty_reply_is_not_a_turn_but_clears_the_prompt(self, env, monkeypatch):
        self._capture(monkeypatch)
        base = {"session_id": "c1", "cwd": str(env.home)}
        hooks.run("prompt", json.dumps({**base, "prompt": "start the migration"}), "codex")
        hooks.run("stop", json.dumps({**base, "last_assistant_message": "  "}), "codex")
        assert not (env.state / "spool").exists()
        assert not list((env.state / "sessions").glob("*.prompt.json")), "a stale prompt must not pair with a later reply"

    def test_a_damaged_kept_prompt_still_makes_a_turn(self, env, monkeypatch):
        self._capture(monkeypatch)
        base = {"session_id": "c1", "cwd": str(env.home)}
        hooks.run("prompt", json.dumps({**base, "prompt": "start the migration"}), "codex")
        kept = next((env.state / "sessions").glob("*.prompt.json"))
        kept.write_text(json.dumps({"prompt": "start the migration", "at": "not a time"}))
        hooks.run("stop", json.dumps({**base, "last_assistant_message": "Migrated."}), "codex")
        [batch] = self._spooled(env)
        assert batch["turns"][0]["started_at"] is None and "Migrated." in batch["turns"][0]["content"]

    def test_unknown_host_is_ignored(self, env, monkeypatch, capsys):
        monkeypatch.setattr(agentd, "request", lambda *a, **k: pytest.fail("unknown host reached the daemon"))
        assert hooks.run("prompt", json.dumps({"prompt": "why is staging failing"}), "nope") == 0
        assert capsys.readouterr().out == ""


class TestStaysOutOfAutomation:
    """Hooks are global: they also fire for every headless `claude -p` —
    scripts, benchmarks, LLM provider calls. On the first real install that
    meant capturing the daemon's own provider prompts (recursion) and loading
    into another session's benchmark calls."""

    def _spy(self, monkeypatch):
        calls = []
        monkeypatch.setattr(agentd, "request", lambda *a, **k: calls.append(a) or None)
        monkeypatch.setattr(agentd, "spawn", lambda cfg: calls.append("spawn"))
        monkeypatch.setattr(agentd, "ensure_running", lambda *a, **k: calls.append("ensure") or False)
        return calls

    def test_headless_sessions_are_left_alone(self, env, monkeypatch):
        calls = self._spy(monkeypatch)
        monkeypatch.setattr(hooks, "headless_session", lambda *_: True)
        for event in ("session-start", "prompt", "stop"):
            hooks.run(event, json.dumps({"prompt": "why is staging failing", "cwd": str(env.home)}))
        assert calls == []

    def test_headless_can_opt_in(self, env, monkeypatch):
        calls = self._spy(monkeypatch)
        monkeypatch.setattr(hooks, "headless_session", lambda *_: True)
        monkeypatch.setenv("ASTROCYTE_HOOKS", "all")
        hooks.run("prompt", json.dumps({"prompt": "why is staging failing", "cwd": str(env.home)}))
        assert calls, "ASTROCYTE_HOOKS=all serves headless sessions"

    def test_provider_scratch_dirs_are_ignored_even_if_hooks_run(self, env, monkeypatch, tmp_path):
        """Older provider code didn't disable hooks; recognise its temp dir."""
        calls = self._spy(monkeypatch)
        scratch = tmp_path / "astrocyte-claude-cli-ab12cd34"
        scratch.mkdir()
        hooks.run("session-start", json.dumps({"cwd": str(scratch)}))
        assert calls == []

    def test_first_capture_of_a_session_takes_only_the_latest_turn(self, env, tmp_path, monkeypatch):
        """A session already running when hooks were installed (Claude Code
        hot-reloads settings) had its whole history captured in one burst."""
        self._spy(monkeypatch)
        t = tmp_path / "t.jsonl"
        t.write_text("".join(human(f"q{i}") + assistant(text(f"a{i}")) for i in range(40)))
        hooks.run("stop", json.dumps({"transcript_path": str(t), "session_id": "old", "cwd": str(env.home)}))
        [spooled] = list((env.state / "spool").glob("*.json"))
        turns = json.loads(spooled.read_text())["turns"]
        assert len(turns) == 1 and "a39" in turns[0]["content"]
        with t.open("a") as fh:
            fh.write(human("q40") + assistant(text("a40")))
        hooks.run("stop", json.dumps({"transcript_path": str(t), "session_id": "old", "cwd": str(env.home)}))
        assert len(list((env.state / "spool").glob("*.json"))) == 2, "later turns are captured normally"

    def test_the_real_ancestry_check_runs_without_error(self):
        assert hooks.headless_session() in (True, False)
        assert hooks.headless_session("codex") in (True, False)

    @pytest.mark.parametrize("argv,headless", [
        (["claude"], False),
        (["claude", "--resume"], False),
        (["claude", "-p", "hi"], True),
        (["/opt/bin/claude", "--print", "--model", "haiku"], True),
    ])
    def test_claude_print_mode_is_headless(self, argv, headless):
        assert hooks.DIALECTS["claude"].headless(argv) is headless

    @pytest.mark.parametrize("argv,headless", [
        (["codex"], False),
        (["codex", "fix the flaky test"], False),  # interactive, with an opening prompt
        (["codex", "resume", "--last"], False),
        (["codex", "app-server"], False),  # backs the desktop app and IDE extension
        (["codex", "exec", "summarise"], True),
        (["codex", "e", "summarise"], True),
        (["codex", "review"], True),
        (["codex", "mcp-server"], True),  # driven by another agent
        (["/opt/homebrew/bin/codex", "-m", "gpt-5.5", "-c", "x=1", "exec", "hi"], True),
        (["codex", "-c", "profile=exec"], False),  # a flag's value is not a subcommand
    ])
    def test_codex_exec_is_headless(self, argv, headless):
        assert hooks.DIALECTS["codex"].headless(argv) is headless


def test_registered_commands_ignore_an_inherited_pythonpath(tmp_path):
    """An agent launched servers and hooks with another checkout on
    PYTHONPATH, so the installed scripts imported the wrong code (observed:
    `invalid choice: 'hook'`). Registrations run the interpreter with -I."""
    from astrocyte.harness.server import ISOLATED, server_spec

    decoy = tmp_path / "decoy" / "astrocyte"
    decoy.mkdir(parents=True)
    (decoy / "__init__.py").write_text("raise SystemExit('decoy astrocyte imported')\n")
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(tmp_path / "decoy")}
    # The mechanism, independent of install mode (editable installs use an
    # import finder that outranks PYTHONPATH; regular installs — the real
    # incident — do not): -I must drop PYTHONPATH from sys.path.
    probe = "import sys, json; print(json.dumps(sys.path))"
    plain = json.loads(subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True).stdout)
    isolated_path = json.loads(
        subprocess.run([sys.executable, ISOLATED, "-c", probe], env=env, capture_output=True, text=True).stdout
    )
    assert str(tmp_path / "decoy") in plain
    assert str(tmp_path / "decoy") not in isolated_path
    isolated = subprocess.run([sys.executable, ISOLATED, "-m", "astrocyte.cli", "--help"], env=env,
                              capture_output=True, text=True)
    assert isolated.returncode == 0 and "setup" in isolated.stdout
    spec = server_spec(sys.executable, Path("/cfg.yaml"))
    assert spec.args[:3] == (ISOLATED, "-m", "astrocyte.mcp")


def test_daemon_never_issues_background_llm_calls(env):
    d = agentd.AgentDaemon(env.cfg)
    assert d.pipeline.enable_multi_query_expansion is False
    assert d.pipeline.enable_observation_consolidation is False


def test_stop_does_not_spool_where_no_daemon_can_drain_it(env, tmp_path, monkeypatch):
    monkeypatch.setattr(agentd, "supported", lambda: False)
    t = tmp_path / "t.jsonl"
    t.write_text(human("q") + assistant(text("a")))
    hooks.run("stop", json.dumps({"transcript_path": str(t), "session_id": "s", "cwd": str(env.home)}))
    assert not (env.state / "spool").exists()


# ── installation into Claude Code ────────────────────────────────────────


CLI = "/opt/astrocyte/bin/astrocyte"


class TestClaudeHookInstall:
    def test_install_is_idempotent_and_keeps_the_users_own_hooks(self, env):
        host = ClaudeCodeHost()
        settings = host.hooks_file()
        settings.parent.mkdir(parents=True)
        mine = {"type": "command", "command": "~/bin/notify.sh"}
        settings.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [mine]}]}}))

        assert host.install_hooks(CLI).status == "installed"
        assert host.install_hooks(CLI).status == "unchanged"
        data = json.loads(settings.read_text())
        assert data["model"] == "opus"
        stop = [h for g in data["hooks"]["Stop"] for h in g["hooks"]]
        assert mine in stop
        ours = next(h for h in stop if "astrocyte" in h["command"])
        # Not async: `claude -p` kills background hooks on exit, so an async
        # capture silently never ran in headless use.
        assert "async" not in ours

    def test_a_moved_install_is_replaced_not_duplicated(self, env):
        host = ClaudeCodeHost()
        host.install_hooks(CLI)
        assert host.install_hooks("/new/place/bin/astrocyte").status == "updated"
        data = json.loads(host.hooks_file().read_text())
        for event in host.HOOK_EVENTS:
            commands = [h["command"] for g in data["hooks"][event] for h in g["hooks"]]
            assert commands == [f"/new/place/bin/astrocyte hook {host.HOOK_EVENTS[event][0]}"]

    def test_paths_with_spaces_are_quoted(self, env):
        from astrocyte.harness.server import hook_prefix

        host = ClaudeCodeHost()
        host.install_hooks(hook_prefix("/Users/Jane Doe/tools/bin/python"))
        assert host.hook_commands()["Stop"] == "'/Users/Jane Doe/tools/bin/python' -I -m astrocyte.cli hook stop"

    def test_uninstall_removes_only_ours(self, env):
        host = ClaudeCodeHost()
        settings = host.hooks_file()
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}}))
        host.install_hooks(CLI)
        assert host.uninstall_hooks().status == "removed"
        assert json.loads(settings.read_text()) == {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}}
        assert host.uninstall_hooks().status == "absent"

    def test_invalid_settings_are_never_overwritten(self, env):
        host = ClaudeCodeHost()
        host.hooks_file().parent.mkdir(parents=True)
        host.hooks_file().write_text("{ broken")
        assert host.install_hooks(CLI).status == "failed"
        assert host.hooks_file().read_text() == "{ broken"
        with pytest.raises(HostConfigError):
            host.hook_commands()


class TestCodexHookInstall:
    """Codex reads hooks from config.toml and hooks.json and warns on every
    run when both hold some (observed: the user's config.toml already had
    another tool's hooks). Ours go where the user's hooks already are."""

    # As another tool writes them into a real config.toml (observed).
    THEIRS = (
        'model = "gpt-5.5"\n\n'
        '[mcp_servers.other]\ncommand = "/bin/other"\nargs = []\n\n'
        "# >>> other-tool SessionStart >>>\n"
        '[[hooks.SessionStart]]\nmatcher = "startup|resume"\n\n'
        '[[hooks.SessionStart.hooks]]\ntype = "command"\ncommand = "\'/bin/other\' hook-augment"\ntimeout = 5\n'
        "# <<< other-tool SessionStart <<<\n"
    )
    WANT = {
        "SessionStart": f"{CLI} hook session-start --host codex",
        "UserPromptSubmit": f"{CLI} hook prompt --host codex",
        "Stop": f"{CLI} hook stop --host codex",
    }

    def _toml(self, host):
        import tomllib

        return tomllib.loads(host.config_file().read_text())

    def test_goes_into_config_toml_beside_the_users_hooks_and_leaves_exactly(self, env):
        host = CodexHost()
        host.config_file().parent.mkdir(parents=True)
        host.config_file().write_text(self.THEIRS)
        assert host.install_hooks(CLI).status == "installed"
        assert host.hooks_file() == host.config_file() and not (env.home / ".codex" / "hooks.json").exists()
        assert host.hook_commands() == self.WANT
        data = self._toml(host)
        assert data["model"] == "gpt-5.5" and data["mcp_servers"]["other"]["command"] == "/bin/other"
        assert [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]] == [
            "'/bin/other' hook-augment", self.WANT["SessionStart"]]
        assert host.install_hooks(CLI).status == "unchanged"
        assert host.uninstall_hooks().status == "removed"
        assert host.config_file().read_text() == self.THEIRS, "uninstall restores the file byte for byte"

    def test_a_fresh_config_gets_one_marked_block(self, env):
        host = CodexHost()
        host.config_file().parent.mkdir(parents=True)
        host.config_file().write_text('model = "o3"\n')
        host.install_hooks(CLI)
        text = host.config_file().read_text()
        assert text.startswith('model = "o3"\n\n# >>> astrocyte hooks >>>') and text.count(">>> astrocyte") == 1
        host.install_hooks("/moved/bin/astrocyte")
        assert host.config_file().read_text().count(">>> astrocyte") == 1
        host.uninstall_hooks()
        assert host.config_file().read_text() == 'model = "o3"\n'

    def test_trust_state_and_other_tables_survive(self, env):
        host = CodexHost()
        host.config_file().parent.mkdir(parents=True)
        original = 'model = "o3"\n\n[hooks.state."x:1"]\ntrusted_hash = "abc"\n\n[tui]\ntheme = "dark"\n'
        host.config_file().write_text(original)
        host.install_hooks(CLI)
        data = self._toml(host)
        assert data["hooks"]["state"] == {"x:1": {"trusted_hash": "abc"}} and data["tui"] == {"theme": "dark"}
        host.uninstall_hooks()
        assert host.config_file().read_text() == original

    def test_users_hooks_json_is_joined_instead(self, env):
        host = CodexHost()
        hooks_json = env.home / ".codex" / "hooks.json"
        hooks_json.parent.mkdir(parents=True)
        mine = {"matcher": "startup", "hooks": [{"type": "command", "command": "~/bin/notes.sh"}]}
        hooks_json.write_text(json.dumps({"hooks": {"SessionStart": [mine]}}))
        assert host.install_hooks(CLI).status == "installed"
        assert host.hooks_file() == hooks_json and not host.config_file().exists()
        assert mine in json.loads(hooks_json.read_text())["hooks"]["SessionStart"]
        host.uninstall_hooks()
        assert json.loads(hooks_json.read_text()) == {"hooks": {"SessionStart": [mine]}}

    def test_an_earlier_hooks_json_install_moves_into_config_toml(self, env):
        host = CodexHost()
        hooks_json = env.home / ".codex" / "hooks.json"
        hooks_json.parent.mkdir(parents=True)
        entry = lambda sub: [{"hooks": [{"type": "command", "command": f"{CLI} hook {sub} --host codex"}]}]  # noqa: E731
        hooks_json.write_text(json.dumps({"hooks": {"SessionStart": entry("session-start"),
                                                    "UserPromptSubmit": entry("prompt"), "Stop": entry("stop")}}))
        host.config_file().write_text(self.THEIRS)
        assert host.install_hooks(CLI).status == "updated"
        assert not hooks_json.exists(), "the file only ever held ours"
        assert host.hook_commands() == self.WANT

    def test_refuses_an_edit_it_cannot_verify(self, env):
        """Events written as inline arrays can't take an appended table."""
        host = CodexHost()
        host.config_file().parent.mkdir(parents=True)
        inline = '[hooks]\nSessionStart = [{ hooks = [{ type = "command", command = "x.sh" }] }]\n'
        host.config_file().write_text(inline)
        outcome = host.install_hooks(CLI)
        assert outcome.status == "failed" and "refusing" in outcome.detail
        assert host.config_file().read_text() == inline

    def test_respects_codex_home(self, env, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
        assert CodexHost().hooks_file() == tmp_path / "codex-home" / "config.toml"

    def test_installed_commands_parse(self, env):
        """The installed command line must be accepted by `astrocyte hook`."""
        from astrocyte.harness.server import hook_prefix

        host = CodexHost()
        host.install_hooks(hook_prefix(sys.executable))
        cmd = host.hook_commands()["Stop"]
        proc = subprocess.run(cmd, shell=True, input="{}", capture_output=True, text=True,
                              env={**os.environ, "ASTROCYTE_HOOKS": "off"})
        assert proc.returncode == 0 and proc.stdout == "", proc.stderr


# ── MCP default bank ─────────────────────────────────────────────────────


def test_stdio_mcp_server_defaults_to_the_project_bank(env, tmp_path):
    """Without it, memory_retain without a bank_id failed — and the agent's
    memories would never meet the ones its hooks capture."""
    repo = tmp_path / "proj"
    repo.mkdir()
    git(repo, "init", "-q")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "memory_banks", "arguments": {}}},
    ]
    proc = subprocess.Popen([sys.executable, "-m", "astrocyte.mcp", "--config", str(env.cfg)], cwd=repo,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for m in msgs:
            proc.stdin.write(json.dumps(m) + "\n")
            proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            assert line, proc.stderr.read()
            reply = json.loads(line)
            if reply.get("id") == 2:
                break
    finally:
        proc.kill()
        proc.wait()
    banks = json.loads(reply["result"]["content"][0]["text"])
    assert banks["default"] == project_bank(str(repo))


# ── Antigravity ──────────────────────────────────────────────────────────


def _agy_step(i: int, kind: str, content, source: str = "MODEL", **extra) -> str:
    return json.dumps({"step_index": i, "source": source, "type": kind, "status": "DONE",
                       "created_at": "2026-10-04T03:03:55Z", "content": content, **extra}) + "\n"


def _agy_user(i: int, text: str) -> str:
    wrapped = f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nlocal time\n</ADDITIONAL_METADATA>"
    return _agy_step(i, "USER_INPUT", wrapped, source="USER_EXPLICIT")


class TestAntigravityTranscript:
    """Step format measured on agy 1.2 and the Antigravity app."""

    def test_turns_are_user_requests_and_prose_replies(self, tmp_path):
        t = tmp_path / "transcript.jsonl"
        t.write_text(
            _agy_user(0, "what is our deploy day?")
            + _agy_step(1, "EPHEMERAL_MESSAGE", "Possibly relevant memories: …", source="SYSTEM_SDK")
            + _agy_step(2, "PLANNER_RESPONSE", None, tool_calls=[{"name": "find_by_name"}])
            + _agy_step(3, "GENERIC", "The command exited with code 0.")
            + _agy_step(4, "PLANNER_RESPONSE", "Tuesdays.")
        )
        [turn], resume = read_new_turns(t, antigravity=True)
        assert turn.user == "what is our deploy day?" and turn.assistant == ["Tuesdays."]
        assert "Possibly relevant" not in turn.render(), "our own injection is never re-captured"
        assert turn.started_at is not None and resume == t.stat().st_size


class TestAntigravityHooks:
    def _payload(self, env, t, conv="conv-1") -> str:
        return json.dumps({"conversationId": conv, "workspacePaths": [str(env.home)], "transcriptPath": str(t),
                           "invocationNum": 0, "initialNumSteps": 1})

    def _wire(self, monkeypatch, calls):
        def fake_request(op, payload=None, **kw):
            calls.append(op)
            return {"context": f"[{op}]"}

        monkeypatch.setattr(agentd, "request", fake_request)
        monkeypatch.setattr(agentd, "ensure_running", lambda cfg, wait: True)
        monkeypatch.setattr(agentd, "spawn", lambda cfg: None)

    def _run(self, monkeypatch, capsys, event, stdin) -> dict:
        import io

        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        assert hooks.main(event, "antigravity") == 0
        return json.loads(capsys.readouterr().out)

    def test_first_model_call_gets_summary_and_recall_later_calls_nothing(self, env, tmp_path, monkeypatch, capsys):
        calls = []
        self._wire(monkeypatch, calls)
        t = tmp_path / "transcript.jsonl"
        t.write_text(_agy_user(0, "why is staging failing today?"))
        out = self._run(monkeypatch, capsys, "prompt", self._payload(env, t))
        assert out == {"injectSteps": [{"ephemeralMessage": "[boot]\n\n[recall]"}]} and calls == ["boot", "recall"]
        # PreInvocation fires before every model call of the turn: act once.
        assert self._run(monkeypatch, capsys, "prompt", self._payload(env, t)) == {}
        with t.open("a") as fh:
            fh.write(_agy_step(1, "PLANNER_RESPONSE", "Checking.") + _agy_user(2, "and the migrations on staging?"))
        out = self._run(monkeypatch, capsys, "prompt", self._payload(env, t))
        assert out == {"injectSteps": [{"ephemeralMessage": "[recall]"}]}, "the summary only once per conversation"

    def test_every_hook_answers_json_even_when_it_stays_out(self, env, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ASTROCYTE_HOOKS", "off")
        assert self._run(monkeypatch, capsys, "stop", "{}") == {}

    def test_stop_captures_the_finished_turn(self, env, tmp_path, monkeypatch):
        monkeypatch.setattr(agentd, "request", lambda *a, **k: None)
        monkeypatch.setattr(agentd, "spawn", lambda cfg: None)
        t = tmp_path / "transcript.jsonl"
        t.write_text(_agy_user(0, "what is our deploy day?") + _agy_step(1, "PLANNER_RESPONSE", "Tuesdays."))
        hooks.run("stop", self._payload(env, t), "antigravity")
        [spooled] = list((env.state / "spool").glob("*.json"))
        batch = json.loads(spooled.read_text())
        assert batch["source"] == "antigravity" and batch["session_id"] == "conv-1"
        assert batch["bank"] == project_bank(str(env.home)) and "Tuesdays." in batch["turns"][0]["content"]

    @pytest.mark.parametrize("argv,headless", [
        (["agy"], False),
        (["agy", "--continue"], False),
        (["/Users/x/.local/bin/agy", "-p", "hi"], True),
        (["agy", "--print", "hi"], True),
    ])
    def test_agy_print_mode_is_headless(self, argv, headless):
        assert hooks.dialect_for("antigravity").headless(argv) is headless


class TestCopilotHooks:
    def test_summary_and_recall_use_copilots_output_key(self, env, monkeypatch, capsys):
        monkeypatch.setattr(agentd, "ensure_running", lambda cfg, wait: True)
        monkeypatch.setattr(agentd, "request", lambda op, payload=None, **k: {"context": f"[{op}]"})
        base = {"sessionId": "cp-1", "timestamp": 1, "cwd": str(env.home)}
        hooks.run("session-start", json.dumps({**base, "source": "startup"}), "copilot")
        assert json.loads(capsys.readouterr().out) == {"additionalContext": "[boot]"}
        hooks.run("prompt", json.dumps({**base, "prompt": "why is staging failing today?"}), "copilot")
        assert json.loads(capsys.readouterr().out) == {"additionalContext": "[recall]"}

    def test_turns_are_not_captured_yet(self, env, tmp_path, monkeypatch):
        monkeypatch.setattr(agentd, "request", lambda *a, **k: None)
        hooks.run("stop", json.dumps({"sessionId": "cp-1", "cwd": str(env.home),
                                      "transcriptPath": str(tmp_path / "t.jsonl")}), "copilot")
        assert not (env.state / "spool").exists()

    def test_node_launched_copilot_print_mode_is_headless(self, monkeypatch):
        def fake_run(argv, **kw):
            table = {100: "50 /bin/sh -c hook", 50: "1 /opt/homebrew/bin/node /opt/lib/node_modules/@github/copilot/index.js -p hi"}
            return subprocess.CompletedProcess(argv, 0, stdout=table.get(int(argv[-1]), ""), stderr="")

        monkeypatch.setattr(hooks.subprocess, "run", fake_run)
        monkeypatch.setattr(hooks.os, "getppid", lambda: 100)
        assert hooks._agent_ancestor_args("copilot") is None, "the script is index.js, not copilot"

        def fake_run2(argv, **kw):
            table = {100: "50 /bin/sh -c hook", 50: "1 node /opt/homebrew/bin/copilot -p hi"}
            return subprocess.CompletedProcess(argv, 0, stdout=table.get(int(argv[-1]), ""), stderr="")

        monkeypatch.setattr(hooks.subprocess, "run", fake_run2)
        assert hooks.headless_session("copilot") is True


class TestNewHookInstalls:
    CLI = "/opt/astrocyte/bin/astrocyte"

    def test_antigravity_is_one_named_entry_beside_the_users_hooks(self, env):
        from astrocyte.harness.hosts import AntigravityHost

        host = AntigravityHost()
        host.hooks_file().parent.mkdir(parents=True)
        theirs = {"lint-checker": {"PostToolUse": [{"matcher": "run_command", "hooks": [{"command": "./lint.sh"}]}]}}
        host.hooks_file().write_text(json.dumps(theirs))
        assert host.install_hooks(self.CLI).status == "installed"
        assert host.install_hooks(self.CLI).status == "unchanged"
        data = json.loads(host.hooks_file().read_text())
        assert data["lint-checker"] == theirs["lint-checker"]
        assert data["astrocyte"]["PreInvocation"][0] == {
            "type": "command", "command": f"{self.CLI} hook prompt --host antigravity", "timeout": 15}
        assert host.uninstall_hooks().status == "removed"
        assert json.loads(host.hooks_file().read_text()) == theirs

    def test_antigravity_mcp_registration(self, env):
        from astrocyte.harness.hosts import AntigravityHost, ServerSpec

        spec = ServerSpec("/py", ("-I", "-m", "astrocyte.mcp"))
        host = AntigravityHost()
        assert host.install(spec).status == "installed"
        assert json.loads(host.config_file().read_text())["mcpServers"]["astrocyte"] == {
            "command": "/py", "args": ["-I", "-m", "astrocyte.mcp"]}

    def test_copilot_hooks_are_a_file_of_their_own(self, env):
        from astrocyte.harness.hosts import CopilotHost

        host = CopilotHost()
        other = env.home / ".copilot" / "hooks" / "codebase-memory-mcp.json"
        other.parent.mkdir(parents=True)
        other.write_text('{"version": 1, "hooks": {}}')
        assert host.install_hooks(self.CLI).status == "installed"
        data = json.loads(host.hooks_file().read_text())
        assert data["version"] == 1 and data["hooks"]["sessionStart"][0]["bash"] == (
            f"{self.CLI} hook session-start --host copilot")
        assert host.install_hooks(self.CLI).status == "unchanged"
        assert host.uninstall_hooks().status == "removed"
        assert not host.hooks_file().exists() and other.read_text() == '{"version": 1, "hooks": {}}'
