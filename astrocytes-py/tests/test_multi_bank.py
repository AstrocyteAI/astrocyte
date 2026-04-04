"""Tests for multi-bank orchestration."""

import pytest

from astrocytes._astrocyte import Astrocyte
from astrocytes.config import AstrocyteConfig
from astrocytes.testing.in_memory import InMemoryEngineProvider
from astrocytes.types import MultiBankStrategy


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

    async def test_cascade_stops_when_enough_hits(self):
        brain, _engine = _make_multi_bank_astrocyte()
        await brain.retain("Only in wide bank", bank_id="org")
        strat = MultiBankStrategy(mode="cascade", min_results_to_stop=1, cascade_order=["personal", "org"])
        result = await brain.recall(
            "wide bank",
            banks=["personal", "org"],
            strategy=strat,
        )
        assert len(result.hits) >= 1
        assert all("wide bank" in h.text or "Only" in h.text for h in result.hits)

    async def test_first_match_returns_first_non_empty_bank(self):
        brain, _engine = _make_multi_bank_astrocyte()
        await brain.retain("Fallback content", bank_id="secondary")
        strat = MultiBankStrategy(mode="first_match", cascade_order=["primary", "secondary"])
        result = await brain.recall(
            "Fallback",
            banks=["primary", "secondary"],
            strategy=strat,
        )
        assert len(result.hits) >= 1
        assert result.hits[0].bank_id == "secondary"

    async def test_parallel_bank_weights_boost_rank(self):
        brain, _engine = _make_multi_bank_astrocyte()
        await brain.retain("keyword alpha secondary", bank_id="low")
        await brain.retain("keyword alpha primary boost", bank_id="high")
        strat = MultiBankStrategy(
            mode="parallel",
            bank_weights={"high": 10.0, "low": 0.1},
        )
        result = await brain.recall(
            "keyword alpha",
            banks=["low", "high"],
            strategy=strat,
        )
        assert result.hits[0].bank_id == "high"


class TestMultiBankStrategyCoercion:
    def test_string_strategy(self):
        from astrocytes._astrocyte import _normalize_multi_bank_strategy

        s = _normalize_multi_bank_strategy("cascade")
        assert s.mode == "cascade"
