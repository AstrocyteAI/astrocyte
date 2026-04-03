"""Tests for multi-bank orchestration."""

import pytest

from astrocytes._astrocyte import Astrocyte
from astrocytes.config import AstrocyteConfig
from astrocytes.testing.in_memory import InMemoryEngineProvider


def _make_multi_bank_astrocyte() -> tuple[Astrocyte, InMemoryEngineProvider]:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


class TestMultiBankRecall:
    async def test_multi_bank_parallel(self):
        brain, engine = _make_multi_bank_astrocyte()
        await brain.retain("Calvin likes dark mode", bank_id="personal")
        await brain.retain("Team uses GitHub Actions", bank_id="team")

        result = await brain.recall("Calvin and team", banks=["personal", "team"])
        # Should find results from both banks
        assert result.total_available >= 1

    async def test_multi_bank_dedup(self):
        brain, engine = _make_multi_bank_astrocyte()
        # Store same content in two banks
        await brain.retain("Shared knowledge", bank_id="bank-1")
        await brain.retain("Shared knowledge", bank_id="bank-2")

        result = await brain.recall("Shared knowledge", banks=["bank-1", "bank-2"])
        # Dedup should remove duplicate text
        texts = [h.text for h in result.hits]
        assert len(texts) == len(set(texts))

    async def test_single_bank_fallback(self):
        brain, engine = _make_multi_bank_astrocyte()
        await brain.retain("Test memory", bank_id="bank-1")

        # Single bank via bank_id parameter
        result = await brain.recall("Test", bank_id="bank-1")
        assert len(result.hits) >= 1

    async def test_no_bank_raises_error(self):
        brain, engine = _make_multi_bank_astrocyte()
        from astrocytes.errors import ConfigError

        with pytest.raises(ConfigError, match="bank_id or banks"):
            await brain.recall("test")
