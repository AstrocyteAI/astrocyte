"""Tests for quota enforcement in the Astrocyte class."""

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import RateLimited
from astrocyte.testing.in_memory import InMemoryEngineProvider


def _make_astrocyte_with_quotas(retain_per_day: int = 3, reflect_per_day: int = 2) -> Astrocyte:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    config.homeostasis.quotas.retain_per_day = retain_per_day
    config.homeostasis.quotas.reflect_per_day = reflect_per_day
    brain = Astrocyte(config)
    brain.set_engine_provider(InMemoryEngineProvider())
    return brain


class TestQuotaEnforcement:
    async def test_retain_quota_enforced(self):
        brain = _make_astrocyte_with_quotas(retain_per_day=2)
        await brain.retain("first", bank_id="b1")
        await brain.retain("second", bank_id="b1")
        with pytest.raises(RateLimited):
            await brain.retain("third", bank_id="b1")

    async def test_reflect_quota_enforced(self):
        brain = _make_astrocyte_with_quotas(reflect_per_day=1)
        await brain.retain("context", bank_id="b1")
        await brain.reflect("query", bank_id="b1")
        with pytest.raises(RateLimited):
            await brain.reflect("second query", bank_id="b1")

    async def test_quota_per_bank_isolation(self):
        brain = _make_astrocyte_with_quotas(retain_per_day=1)
        await brain.retain("first", bank_id="b1")
        # Different bank — should be independent
        await brain.retain("first", bank_id="b2")

    async def test_no_quota_allows_unlimited(self):
        config = AstrocyteConfig()
        config.provider = "test"
        config.barriers.pii.mode = "disabled"
        # No quotas set (defaults to None)
        brain = Astrocyte(config)
        brain.set_engine_provider(InMemoryEngineProvider())

        for i in range(20):
            await brain.retain(f"memory {i}", bank_id="b1")
        # Should not raise
