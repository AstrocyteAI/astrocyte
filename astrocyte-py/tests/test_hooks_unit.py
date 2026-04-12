"""Unit tests for HookManager — registration, dispatch, async, error handling."""

from __future__ import annotations

import pytest

from astrocyte._hooks import HookManager
from astrocyte.policy.observability import StructuredLogger
from astrocyte.types import HookEvent


def _make_manager() -> HookManager:
    return HookManager(StructuredLogger(level="WARNING"))


class TestRegistration:
    @pytest.mark.asyncio
    async def test_register_and_fire(self) -> None:
        """Registered sync handlers are called."""
        mgr = _make_manager()
        events: list[HookEvent] = []
        mgr.register("on_retain", lambda e: events.append(e))

        await mgr.fire("on_retain", bank_id="b1", data={"key": "val"})
        assert len(events) == 1
        assert events[0].type == "on_retain"
        assert events[0].bank_id == "b1"
        assert events[0].data == {"key": "val"}

    @pytest.mark.asyncio
    async def test_multiple_handlers(self) -> None:
        mgr = _make_manager()
        calls: list[str] = []
        mgr.register("on_retain", lambda e: calls.append("h1"))
        mgr.register("on_retain", lambda e: calls.append("h2"))

        await mgr.fire("on_retain")
        assert calls == ["h1", "h2"]

    @pytest.mark.asyncio
    async def test_different_event_types_isolated(self) -> None:
        mgr = _make_manager()
        retain_calls: list[str] = []
        recall_calls: list[str] = []
        mgr.register("on_retain", lambda e: retain_calls.append("r"))
        mgr.register("on_recall", lambda e: recall_calls.append("r"))

        await mgr.fire("on_retain")
        assert len(retain_calls) == 1
        assert len(recall_calls) == 0


class TestAsyncHandlers:
    @pytest.mark.asyncio
    async def test_async_handler_called(self) -> None:
        mgr = _make_manager()
        events: list[HookEvent] = []

        async def async_handler(e: HookEvent) -> None:
            events.append(e)

        mgr.register("on_retain", async_handler)
        await mgr.fire("on_retain", bank_id="b1")
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_mixed_sync_and_async(self) -> None:
        mgr = _make_manager()
        calls: list[str] = []

        mgr.register("on_retain", lambda e: calls.append("sync"))

        async def async_handler(e: HookEvent) -> None:
            calls.append("async")

        mgr.register("on_retain", async_handler)
        await mgr.fire("on_retain")
        assert calls == ["sync", "async"]


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_failing_handler_does_not_break_dispatch(self) -> None:
        mgr = _make_manager()
        calls: list[str] = []

        def bad_handler(e: HookEvent) -> None:
            raise RuntimeError("boom")

        mgr.register("on_retain", bad_handler)
        mgr.register("on_retain", lambda e: calls.append("ok"))

        # Should not raise
        await mgr.fire("on_retain", bank_id="b1")
        assert calls == ["ok"]

    @pytest.mark.asyncio
    async def test_failing_async_handler_logged(self) -> None:
        mgr = _make_manager()
        calls: list[str] = []

        async def bad_async(e: HookEvent) -> None:
            raise ValueError("async boom")

        mgr.register("on_retain", bad_async)
        mgr.register("on_retain", lambda e: calls.append("ok"))

        await mgr.fire("on_retain")
        assert calls == ["ok"]


class TestNoHandlers:
    @pytest.mark.asyncio
    async def test_fire_with_no_handlers(self) -> None:
        mgr = _make_manager()
        # Should not raise
        await mgr.fire("on_retain", bank_id="b1", data={"x": 1})

    @pytest.mark.asyncio
    async def test_fire_unknown_event_type(self) -> None:
        mgr = _make_manager()
        mgr.register("on_retain", lambda e: None)
        # Firing a different type should be a no-op
        await mgr.fire("on_forget")


class TestHookEventFields:
    @pytest.mark.asyncio
    async def test_event_has_required_fields(self) -> None:
        mgr = _make_manager()
        captured: list[HookEvent] = []
        mgr.register("on_recall", lambda e: captured.append(e))

        await mgr.fire("on_recall", bank_id="test-bank", data={"count": 5})
        event = captured[0]
        assert event.event_id  # non-empty
        assert event.type == "on_recall"
        assert event.timestamp is not None
        assert event.bank_id == "test-bank"
        assert event.data == {"count": 5}

    @pytest.mark.asyncio
    async def test_event_without_optional_fields(self) -> None:
        mgr = _make_manager()
        captured: list[HookEvent] = []
        mgr.register("on_retain", lambda e: captured.append(e))

        await mgr.fire("on_retain")
        event = captured[0]
        assert event.bank_id is None
        assert event.data is None
