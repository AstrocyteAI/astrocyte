"""Tests for hook dispatch in Astrocyte class."""

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.testing.in_memory import InMemoryEngineProvider
from astrocyte.types import HookEvent


def _make_astrocyte_with_engine() -> tuple[Astrocyte, InMemoryEngineProvider]:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


class TestHookDispatch:
    async def test_on_retain_fires(self):
        brain, engine = _make_astrocyte_with_engine()
        events: list[HookEvent] = []
        brain.register_hook("on_retain", lambda e: events.append(e))

        await brain.retain("test content", bank_id="b1")

        assert len(events) == 1
        assert events[0].type == "on_retain"
        assert events[0].bank_id == "b1"
        assert events[0].data is not None

    async def test_on_recall_fires(self):
        brain, engine = _make_astrocyte_with_engine()
        events: list[HookEvent] = []
        brain.register_hook("on_recall", lambda e: events.append(e))

        await brain.retain("dark mode", bank_id="b1")
        await brain.recall("dark", bank_id="b1")

        assert len(events) == 1
        assert events[0].type == "on_recall"

    async def test_on_reflect_fires(self):
        brain, engine = _make_astrocyte_with_engine()
        events: list[HookEvent] = []
        brain.register_hook("on_reflect", lambda e: events.append(e))

        await brain.retain("Calvin likes Python", bank_id="b1")
        await brain.reflect("What does Calvin like?", bank_id="b1")

        assert len(events) == 1
        assert events[0].type == "on_reflect"

    async def test_on_forget_fires(self):
        brain, engine = _make_astrocyte_with_engine()
        events: list[HookEvent] = []
        brain.register_hook("on_forget", lambda e: events.append(e))

        result = await brain.retain("to delete", bank_id="b1")
        await brain.forget("b1", memory_ids=[result.memory_id])

        assert len(events) == 1
        assert events[0].type == "on_forget"
        assert events[0].data["deleted_count"] >= 1

    async def test_on_pii_detected_fires(self):
        config = AstrocyteConfig()
        config.provider = "test"
        config.barriers.pii.mode = "regex"
        config.barriers.pii.action = "redact"
        brain = Astrocyte(config)
        brain.set_engine_provider(InMemoryEngineProvider())

        events: list[HookEvent] = []
        brain.register_hook("on_pii_detected", lambda e: events.append(e))

        await brain.retain("Email: user@example.com", bank_id="b1")

        assert len(events) == 1
        assert events[0].type == "on_pii_detected"
        assert "email" in (events[0].data or {}).get("pii_types", "")

    async def test_async_hook_handler(self):
        brain, engine = _make_astrocyte_with_engine()
        events: list[str] = []

        async def async_handler(event: HookEvent) -> None:
            events.append(f"async:{event.type}")

        brain.register_hook("on_retain", async_handler)
        await brain.retain("test", bank_id="b1")

        assert events == ["async:on_retain"]

    async def test_failing_hook_does_not_break_operation(self):
        brain, engine = _make_astrocyte_with_engine()

        def bad_hook(event: HookEvent) -> None:
            raise RuntimeError("hook exploded")

        brain.register_hook("on_retain", bad_hook)

        # Operation should succeed despite hook failure
        result = await brain.retain("test", bank_id="b1")
        assert result.stored is True

    async def test_multiple_hooks_all_fire(self):
        brain, engine = _make_astrocyte_with_engine()
        calls: list[int] = []
        brain.register_hook("on_retain", lambda e: calls.append(1))
        brain.register_hook("on_retain", lambda e: calls.append(2))
        brain.register_hook("on_retain", lambda e: calls.append(3))

        await brain.retain("test", bank_id="b1")

        assert calls == [1, 2, 3]
