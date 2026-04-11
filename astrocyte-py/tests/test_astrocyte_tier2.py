"""Tests for Astrocyte class with Tier 2 engine provider.

Regression target for ``provider_tier: engine`` + :class:`~astrocyte.provider.EngineProvider`.
For Tier-2 + Tier-1 **hybrid** merge behavior see ``tests/test_hybrid_engine.py`` and
``astrocyte.hybrid.HybridEngineProvider``.
"""

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import AccessDenied, CapabilityNotSupported, RateLimited
from astrocyte.testing.in_memory import InMemoryEngineProvider
from astrocyte.types import AccessGrant, AstrocyteContext


def _make_astrocyte(
    engine: InMemoryEngineProvider | None = None,
    rate_limit_retain: int | None = None,
    pii_mode: str = "regex",
    pii_action: str = "redact",
    access_enabled: bool = False,
) -> Astrocyte:
    """Helper to create a configured Astrocyte with an engine provider."""
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = pii_mode
    config.barriers.pii.action = pii_action
    if rate_limit_retain:
        config.homeostasis.rate_limits.retain_per_minute = rate_limit_retain
    config.access_control.enabled = access_enabled
    config.access_control.default_policy = "deny"

    brain = Astrocyte(config)
    engine = engine or InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain


class TestTier2Retain:
    async def test_basic_retain(self):
        brain = _make_astrocyte()
        result = await brain.retain("Hello world", bank_id="bank-1")
        assert result.stored is True
        assert result.memory_id is not None

    async def test_retain_with_metadata(self):
        brain = _make_astrocyte()
        result = await brain.retain("Hello", bank_id="bank-1", metadata={"key": "value"}, tags=["test"])
        assert result.stored is True

    async def test_pii_redaction(self):
        engine = InMemoryEngineProvider()
        brain = _make_astrocyte(engine=engine, pii_action="redact")
        result = await brain.retain("Contact user@example.com", bank_id="bank-1")
        assert result.stored is True
        # Check that the stored content was redacted
        memories = engine._memories.get("bank-1", [])
        assert len(memories) == 1
        assert "user@example.com" not in memories[0].text
        assert "[EMAIL_REDACTED]" in memories[0].text

    async def test_empty_content_rejected(self):
        brain = _make_astrocyte()
        result = await brain.retain("", bank_id="bank-1")
        assert result.stored is False
        assert "empty" in result.error.lower()

    async def test_rate_limiting(self):
        brain = _make_astrocyte(rate_limit_retain=2)
        await brain.retain("First", bank_id="bank-1")
        await brain.retain("Second", bank_id="bank-1")
        with pytest.raises(RateLimited):
            await brain.retain("Third", bank_id="bank-1")


class TestTier2Recall:
    async def test_basic_recall(self):
        brain = _make_astrocyte()
        await brain.retain("Calvin prefers dark mode", bank_id="bank-1")
        result = await brain.recall("dark mode", bank_id="bank-1")
        assert len(result.hits) >= 1
        assert "dark mode" in result.hits[0].text.lower()

    async def test_recall_empty_bank(self):
        brain = _make_astrocyte()
        result = await brain.recall("anything", bank_id="empty-bank")
        assert result.hits == []

    async def test_recall_max_results(self):
        brain = _make_astrocyte()
        for i in range(10):
            await brain.retain(f"Memory number {i} about testing", bank_id="bank-1")
        result = await brain.recall("testing", bank_id="bank-1", max_results=3)
        assert len(result.hits) <= 3


class TestTier2Reflect:
    async def test_reflect_supported(self):
        brain = _make_astrocyte(engine=InMemoryEngineProvider(supports_reflect=True))
        await brain.retain("Calvin likes Python", bank_id="bank-1")
        result = await brain.reflect("What does Calvin like?", bank_id="bank-1")
        assert result.answer
        assert len(result.answer) > 0

    async def test_reflect_not_supported_error(self):
        engine = InMemoryEngineProvider(supports_reflect=False)
        config = AstrocyteConfig()
        config.provider = "test"
        config.fallback_strategy = "error"
        brain = Astrocyte(config)
        brain.set_engine_provider(engine)

        with pytest.raises(CapabilityNotSupported, match="reflect"):
            await brain.reflect("test", bank_id="bank-1")


class TestTier2Forget:
    async def test_forget_by_ids(self):
        engine = InMemoryEngineProvider()
        brain = _make_astrocyte(engine=engine)
        result = await brain.retain("test memory", bank_id="bank-1")
        mem_id = result.memory_id

        forget_result = await brain.forget("bank-1", memory_ids=[mem_id])
        assert forget_result.deleted_count >= 1

    async def test_forget_all(self):
        engine = InMemoryEngineProvider()
        brain = _make_astrocyte(engine=engine)
        await brain.retain("memory 1", bank_id="bank-1")
        await brain.retain("memory 2", bank_id="bank-1")

        from astrocyte.types import ForgetRequest

        # Use internal method for scope="all"
        result = await brain._do_forget(ForgetRequest(bank_id="bank-1", scope="all"))
        assert result.deleted_count == 2


class TestAccessControl:
    async def test_access_denied(self):
        brain = _make_astrocyte(access_enabled=True)
        ctx = AstrocyteContext(principal="agent:unauthorized")
        with pytest.raises(AccessDenied):
            await brain.retain("test", bank_id="bank-1", context=ctx)

    async def test_access_granted(self):
        brain = _make_astrocyte(access_enabled=True)
        brain.set_access_grants(
            [
                AccessGrant(bank_id="bank-1", principal="agent:bot", permissions=["read", "write"]),
            ]
        )
        ctx = AstrocyteContext(principal="agent:bot")
        result = await brain.retain("test", bank_id="bank-1", context=ctx)
        assert result.stored is True

    async def test_wildcard_principal(self):
        brain = _make_astrocyte(access_enabled=True)
        brain.set_access_grants(
            [
                AccessGrant(bank_id="bank-1", principal="*", permissions=["read"]),
            ]
        )
        ctx = AstrocyteContext(principal="agent:anyone")
        result = await brain.recall("test", bank_id="bank-1", context=ctx)
        assert isinstance(result.hits, list)

    async def test_wildcard_bank(self):
        brain = _make_astrocyte(access_enabled=True)
        brain.set_access_grants(
            [
                AccessGrant(bank_id="*", principal="agent:admin", permissions=["read", "write", "forget", "admin"]),
            ]
        )
        ctx = AstrocyteContext(principal="agent:admin")
        result = await brain.retain("test", bank_id="any-bank", context=ctx)
        assert result.stored is True
