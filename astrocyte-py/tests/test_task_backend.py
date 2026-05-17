"""Tests for pluggable background task backends."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrocyte.task_backend import (
    AsyncFireAndTrackBackend,
    SyncTaskBackend,
    get_default_task_backend,
    set_default_task_backend,
)

# ─── SyncTaskBackend ───────────────────────────────────────────────────


class TestSyncTaskBackend:
    @pytest.mark.asyncio
    async def test_executes_inline(self) -> None:
        seen: list[dict[str, Any]] = []

        async def executor(task: dict[str, Any]) -> None:
            seen.append(task)

        backend = SyncTaskBackend()
        backend.set_executor(executor)
        await backend.initialize()
        await backend.submit({"type": "ingest", "x": 1})
        await backend.shutdown()
        assert seen == [{"type": "ingest", "x": 1}]

    @pytest.mark.asyncio
    async def test_no_executor_logs_and_continues(self) -> None:
        backend = SyncTaskBackend()
        await backend.initialize()
        # No set_executor → submit logs warning and returns without raising
        await backend.submit({"type": "ingest"})

    @pytest.mark.asyncio
    async def test_executor_exception_swallowed(self) -> None:
        async def bad_executor(task: dict[str, Any]) -> None:
            raise RuntimeError("kaboom")

        backend = SyncTaskBackend()
        backend.set_executor(bad_executor)
        await backend.initialize()
        # Must not raise — error is logged, swallowed
        await backend.submit({"type": "ingest"})


# ─── AsyncFireAndTrackBackend ─────────────────────────────────────────


class TestAsyncFireAndTrackBackend:
    @pytest.mark.asyncio
    async def test_submit_spawns_and_completes(self) -> None:
        events: list[str] = []

        async def executor(task: dict[str, Any]) -> None:
            await asyncio.sleep(0.01)
            events.append(task["type"])

        backend = AsyncFireAndTrackBackend()
        backend.set_executor(executor)
        await backend.initialize()
        await backend.submit({"type": "a"})
        await backend.submit({"type": "b"})
        # Submit returns immediately; events not yet recorded
        assert events == []
        await backend.shutdown()
        # Shutdown drains both
        assert sorted(events) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_inflight_count_tracks(self) -> None:
        block = asyncio.Event()

        async def executor(task: dict[str, Any]) -> None:
            await block.wait()

        backend = AsyncFireAndTrackBackend()
        backend.set_executor(executor)
        await backend.initialize()
        await backend.submit({"type": "x"})
        await backend.submit({"type": "y"})
        # Give the event loop a tick to spawn
        await asyncio.sleep(0)
        assert backend.inflight_count == 2
        block.set()
        await backend.shutdown()
        assert backend.inflight_count == 0

    @pytest.mark.asyncio
    async def test_shutdown_timeout(self) -> None:
        block = asyncio.Event()

        async def hanging_executor(task: dict[str, Any]) -> None:
            await block.wait()  # never set

        backend = AsyncFireAndTrackBackend(shutdown_timeout_seconds=0.1)
        backend.set_executor(hanging_executor)
        await backend.initialize()
        await backend.submit({"type": "hang"})
        await asyncio.sleep(0)
        await backend.shutdown()  # logs timeout warning, returns
        # backend marked uninitialized after shutdown
        assert not backend._initialized
        # Clean up the hanging task to avoid event-loop warnings
        block.set()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_executor_exception_does_not_crash_backend(self) -> None:
        async def bad_executor(task: dict[str, Any]) -> None:
            raise ValueError("nope")

        backend = AsyncFireAndTrackBackend()
        backend.set_executor(bad_executor)
        await backend.initialize()
        await backend.submit({"type": "a"})
        await backend.submit({"type": "b"})
        await backend.shutdown()
        # No exception; in-flight set drained
        assert backend.inflight_count == 0


# ─── default singleton ────────────────────────────────────────────────


class TestDefaultBackend:
    @pytest.mark.asyncio
    async def test_default_is_sync(self) -> None:
        # Save and restore so other tests aren't affected
        backend = get_default_task_backend()
        assert isinstance(backend, SyncTaskBackend)

    def test_set_default_persists(self) -> None:
        custom = AsyncFireAndTrackBackend()
        set_default_task_backend(custom)
        assert get_default_task_backend() is custom
        set_default_task_backend(SyncTaskBackend())  # restore
