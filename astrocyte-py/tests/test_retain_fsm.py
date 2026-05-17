"""M14.0: unit tests for the retain FSM engine + checkpoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from astrocyte.pipeline.retain_fsm import (
    Complete,
    Failed,
    FileCheckpoint,
    InMemoryCheckpoint,
    Parallel,
    RetainContext,
    RetainFSM,
    RetainServices,
    register_default_states,
)


def _fsm() -> RetainFSM:
    """Build an FSM with fake services (states won't touch them)."""
    services = RetainServices(store=MagicMock(), provider=MagicMock())
    return RetainFSM(services)


def _ctx(**kw) -> RetainContext:
    defaults = {"bank_id": "b1", "source_id": "s1", "md_text": "# doc"}
    defaults.update(kw)
    return RetainContext(**defaults)


# ── Engine: single state + termination ─────────────────────────────────


class TestSingleState:
    async def test_complete_state_terminates(self) -> None:
        fsm = _fsm()

        async def state_done(ctx, services) -> Complete:
            return Complete()

        fsm.register("INIT", state_done)
        ctx = await fsm.run(_ctx())
        assert ctx.completed_at is not None
        assert ctx.errors == []
        assert ctx.last_state == "INIT"
        assert len(ctx.step_log) == 1
        assert ctx.step_log[0].state == "INIT"
        assert ctx.step_log[0].error is None
        assert ctx.step_log[0].duration_ms is not None

    async def test_failed_state_terminates_with_error(self) -> None:
        fsm = _fsm()

        async def state_bad(ctx, services) -> Failed:
            return Failed("ingest unavailable")

        fsm.register("INIT", state_bad)
        ctx = await fsm.run(_ctx())
        assert ctx.completed_at is None
        assert ctx.errors == ["INIT: ingest unavailable"]

    async def test_unknown_state_terminates_with_error(self) -> None:
        fsm = _fsm()
        ctx = await fsm.run(_ctx(), initial_state="NO_SUCH_STATE")
        assert ctx.completed_at is None
        assert any("unknown state" in e for e in ctx.errors)


# ── Engine: chained states ─────────────────────────────────────────────


class TestChainedStates:
    async def test_state_chain_terminates(self) -> None:
        fsm = _fsm()

        async def s_init(ctx, services) -> str:
            ctx.entities.append("seen-INIT")
            return "READ"

        async def s_read(ctx, services) -> str:
            ctx.entities.append("seen-READ")
            return "COMPLETE"

        async def s_complete(ctx, services) -> Complete:
            ctx.entities.append("seen-COMPLETE")
            return Complete()

        fsm.register("INIT", s_init)
        fsm.register("READ", s_read)
        fsm.register("COMPLETE", s_complete)
        ctx = await fsm.run(_ctx())
        assert ctx.completed_at is not None
        assert ctx.entities == ["seen-INIT", "seen-READ", "seen-COMPLETE"]
        assert [e.state for e in ctx.step_log] == ["INIT", "READ", "COMPLETE"]

    async def test_default_states_register_and_drive_to_complete(self) -> None:
        fsm = _fsm()
        register_default_states(fsm)
        ctx = await fsm.run(_ctx())
        assert ctx.completed_at is not None
        assert ctx.errors == []
        # Default INIT validates inputs, READ is no-op pass-through to COMPLETE
        assert {e.state for e in ctx.step_log} == {"INIT", "READ", "COMPLETE"}


# ── Engine: error handling ─────────────────────────────────────────────


class TestErrorHandling:
    async def test_state_raises_exception_captured(self) -> None:
        fsm = _fsm()

        async def s_boom(ctx, services):
            raise RuntimeError("db is on fire")

        fsm.register("INIT", s_boom)
        ctx = await fsm.run(_ctx())
        assert ctx.completed_at is None
        assert len(ctx.errors) == 1
        assert "RuntimeError" in ctx.errors[0]
        assert "db is on fire" in ctx.errors[0]
        # Step log captures the failed entry too
        assert ctx.step_log[-1].error is not None

    async def test_state_returns_garbage_terminates(self) -> None:
        fsm = _fsm()

        async def s_garbage(ctx, services):
            return 42  # not a state name, Complete, Failed, or Parallel

        fsm.register("INIT", s_garbage)
        ctx = await fsm.run(_ctx())
        assert any("unsupported type" in e for e in ctx.errors)


# ── Engine: parallel branches + join ───────────────────────────────────


class TestParallelBranches:
    async def test_parallel_join(self) -> None:
        fsm = _fsm()

        async def s_init(ctx, services) -> Parallel:
            return Parallel(branches=("A", "B", "C"), join="DONE")

        async def s_a(ctx, services):
            ctx.entities.append("A")
            return Complete()  # branch result is ignored — join is fixed

        async def s_b(ctx, services):
            ctx.entities.append("B")
            return Complete()

        async def s_c(ctx, services):
            ctx.entities.append("C")
            return Complete()

        async def s_done(ctx, services) -> Complete:
            ctx.entities.append("DONE")
            return Complete()

        fsm.register("INIT", s_init)
        fsm.register("A", s_a)
        fsm.register("B", s_b)
        fsm.register("C", s_c)
        fsm.register("DONE", s_done)
        ctx = await fsm.run(_ctx())
        assert ctx.completed_at is not None
        # All branches ran (order not guaranteed since they're parallel).
        assert set(ctx.entities[:3]) == {"A", "B", "C"}
        # Join state ran after them
        assert ctx.entities[-1] == "DONE"
        # Step log shows parallel branches tagged
        states = [e.state for e in ctx.step_log]
        assert "INIT" in states
        assert "DONE" in states
        assert {"parallel:A", "parallel:B", "parallel:C"} <= set(states)

    async def test_parallel_branch_failure_terminates(self) -> None:
        fsm = _fsm()

        async def s_init(ctx, services) -> Parallel:
            return Parallel(branches=("A", "B"), join="DONE")

        async def s_a(ctx, services):
            return Complete()

        async def s_b(ctx, services):
            return Failed("B blew up")

        async def s_done(ctx, services) -> Complete:
            return Complete()

        fsm.register("INIT", s_init)
        fsm.register("A", s_a)
        fsm.register("B", s_b)
        fsm.register("DONE", s_done)
        ctx = await fsm.run(_ctx())
        assert ctx.completed_at is None
        assert any("parallel:B" in e and "B blew up" in e for e in ctx.errors)

    async def test_parallel_unknown_branch(self) -> None:
        fsm = _fsm()

        async def s_init(ctx, services) -> Parallel:
            return Parallel(branches=("MISSING",), join="DONE")

        async def s_done(ctx, services) -> Complete:
            return Complete()

        fsm.register("INIT", s_init)
        fsm.register("DONE", s_done)
        ctx = await fsm.run(_ctx())
        assert ctx.completed_at is None
        assert any("unknown parallel branch" in e for e in ctx.errors)


# ── Default states: input validation ───────────────────────────────────


class TestDefaultStates:
    async def test_init_rejects_empty_bank(self) -> None:
        fsm = _fsm()
        register_default_states(fsm)
        ctx = _ctx(bank_id="")
        ctx = await fsm.run(ctx)
        assert ctx.completed_at is None
        assert any("bank_id is required" in e for e in ctx.errors)

    async def test_init_rejects_empty_md(self) -> None:
        fsm = _fsm()
        register_default_states(fsm)
        ctx = _ctx(md_text="   ")
        ctx = await fsm.run(ctx)
        assert ctx.completed_at is None
        assert any("md_text is empty" in e for e in ctx.errors)


# ── Checkpoint: in-memory round-trip ───────────────────────────────────


class TestInMemoryCheckpoint:
    async def test_save_load_roundtrip(self) -> None:
        cp = InMemoryCheckpoint()
        ctx = _ctx()
        ctx.document_id = "doc-1"
        ctx.entities = ["A", "B"]
        ctx.wikis_created = ["w1"]
        ctx.supersedes_edges = [("f1", "f2")]
        ctx.last_state = "EMBED"
        ctx.errors = ["something"]
        await cp.save(ctx)

        loaded = await cp.load("b1", "s1")
        assert loaded is not None
        assert loaded.bank_id == "b1"
        assert loaded.source_id == "s1"
        assert loaded.document_id == "doc-1"
        assert loaded.entities == ["A", "B"]
        assert loaded.wikis_created == ["w1"]
        assert loaded.supersedes_edges == [("f1", "f2")]
        assert loaded.last_state == "EMBED"
        assert loaded.errors == ["something"]

    async def test_load_missing_returns_none(self) -> None:
        cp = InMemoryCheckpoint()
        assert await cp.load("b1", "s1") is None

    async def test_delete(self) -> None:
        cp = InMemoryCheckpoint()
        await cp.save(_ctx())
        # Hoist awaits out of asserts so the delete still runs under
        # `python -O` (which strips assertions).
        first = await cp.delete("b1", "s1")
        assert first is True
        second = await cp.delete("b1", "s1")
        assert second is False
        assert await cp.load("b1", "s1") is None


# ── Checkpoint: file backend ───────────────────────────────────────────


class TestFileCheckpoint:
    async def test_file_save_load(self, tmp_path: Path) -> None:
        cp = FileCheckpoint(tmp_path)
        ctx = _ctx()
        ctx.document_id = "doc-42"
        ctx.entities = ["X"]
        await cp.save(ctx)

        loaded = await cp.load("b1", "s1")
        assert loaded is not None
        assert loaded.document_id == "doc-42"
        assert loaded.entities == ["X"]

    async def test_file_sanitises_bank_and_source(self, tmp_path: Path) -> None:
        cp = FileCheckpoint(tmp_path)
        # bank_id and source_id with shell-unsafe chars
        ctx = _ctx(bank_id="tenant/with/slash", source_id="user:with:colons")
        await cp.save(ctx)
        # Saving + loading should round-trip cleanly
        loaded = await cp.load("tenant/with/slash", "user:with:colons")
        assert loaded is not None
        assert loaded.bank_id == "tenant/with/slash"

    async def test_file_load_missing_returns_none(self, tmp_path: Path) -> None:
        cp = FileCheckpoint(tmp_path)
        assert await cp.load("nope", "nope") is None

    async def test_file_malformed_json_returns_none(self, tmp_path: Path) -> None:
        cp = FileCheckpoint(tmp_path)
        # Write garbage to the expected path
        await cp.save(_ctx())  # creates the dir structure
        # Find the file and corrupt it
        path = cp._path_for("b1", "s1")
        path.write_text("not valid json {{{")
        assert await cp.load("b1", "s1") is None

    async def test_file_delete(self, tmp_path: Path) -> None:
        cp = FileCheckpoint(tmp_path)
        await cp.save(_ctx())
        # Hoist awaits out of asserts so the delete still runs under
        # `python -O` (which strips assertions).
        first = await cp.delete("b1", "s1")
        assert first is True
        second = await cp.delete("b1", "s1")
        assert second is False


# ── Engine + checkpoint integration: resume ────────────────────────────


class TestResume:
    async def test_resume_picks_up_at_last_state(self) -> None:
        cp = InMemoryCheckpoint()
        fsm = _fsm()
        visited: list[str] = []

        async def s_init(ctx, services) -> str:
            visited.append("INIT")
            return "READ"

        async def s_read(ctx, services) -> str:
            visited.append("READ")
            ctx.entities.append("read-output")
            return "STAGE3"

        async def s_stage3(ctx, services) -> Complete:
            visited.append("STAGE3")
            return Complete()

        fsm.register("INIT", s_init)
        fsm.register("READ", s_read)
        fsm.register("STAGE3", s_stage3)

        # First run, no failures
        ctx = await fsm.run(_ctx(), checkpoint=cp)
        assert ctx.completed_at is not None
        assert visited == ["INIT", "READ", "STAGE3"]

        # Reload from checkpoint and resume — should re-run STAGE3 only
        # (since last_state == "COMPLETE" after success, we explicitly
        # rewind to test resume semantics).
        loaded = await cp.load("b1", "s1")
        assert loaded is not None
        loaded.last_state = "STAGE3"
        loaded.completed_at = None  # pretend we crashed before completing
        visited.clear()
        ctx2 = await fsm.resume(loaded, checkpoint=cp)
        assert ctx2.completed_at is not None
        assert visited == ["STAGE3"]  # only STAGE3 re-ran

    async def test_checkpoint_saved_on_failure(self) -> None:
        cp = InMemoryCheckpoint()
        fsm = _fsm()

        async def s_init(ctx, services) -> str:
            ctx.entities.append("init-done")
            return "READ"

        async def s_read(ctx, services):
            raise RuntimeError("boom")

        fsm.register("INIT", s_init)
        fsm.register("READ", s_read)

        ctx = await fsm.run(_ctx(), checkpoint=cp)
        assert ctx.completed_at is None
        assert "RuntimeError" in ctx.errors[0]

        # The failure should still have been checkpointed
        loaded = await cp.load("b1", "s1")
        assert loaded is not None
        assert loaded.entities == ["init-done"]
        assert "RuntimeError" in loaded.errors[0]


# ── Context helpers ────────────────────────────────────────────────────


class TestContextHelpers:
    async def test_duration_by_state(self) -> None:
        fsm = _fsm()

        async def s_a(ctx, services) -> str:
            return "B"

        async def s_b(ctx, services) -> Complete:
            return Complete()

        fsm.register("INIT", s_a)
        fsm.register("A", s_a)
        fsm.register("B", s_b)

        ctx = await fsm.run(_ctx(), initial_state="INIT")
        durations = ctx.duration_ms_by_state()
        # INIT and B are the states that were actually invoked (INIT
        # registered to s_a which returns "B"). A is unused.
        assert "INIT" in durations
        assert "B" in durations
        # Each duration is a positive float
        for v in durations.values():
            assert v >= 0.0


# Mark all tests as asyncio
pytestmark = pytest.mark.asyncio
