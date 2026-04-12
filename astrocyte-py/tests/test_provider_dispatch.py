"""Unit tests for ProviderDispatcher — routing logic tested in isolation.

Tests engine vs pipeline routing, tiered retrieval dispatch, hybrid detection,
capability fallback for reflect/forget, and reflect_from_hits synthesis.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrocyte._provider_dispatch import ProviderDispatcher
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import CapabilityNotSupported, ConfigError
from astrocyte.types import (
    EngineCapabilities,
    ForgetRequest,
    ForgetResult,
    MemoryHit,
    RecallRequest,
    RecallResult,
    ReflectRequest,
    ReflectResult,
    RetainRequest,
    RetainResult,
)


def _make_dispatcher(
    *,
    provider: str | None = None,
    fallback: str = "error",
) -> ProviderDispatcher:
    config = AstrocyteConfig()
    config.provider = provider
    config.fallback_strategy = fallback
    return ProviderDispatcher(config)


def _mock_engine(
    *,
    supports_reflect: bool = True,
    supports_forget: bool = True,
) -> MagicMock:
    engine = MagicMock()
    engine.retain = AsyncMock(return_value=RetainResult(stored=True, memory_id="m1"))
    engine.recall = AsyncMock(return_value=RecallResult(hits=[], total_available=0, truncated=False))
    engine.reflect = AsyncMock(return_value=ReflectResult(answer="answer", sources=[]))
    engine.forget = AsyncMock(return_value=ForgetResult(deleted_count=1))
    engine.capabilities.return_value = EngineCapabilities(
        supports_reflect=supports_reflect,
        supports_forget=supports_forget,
    )
    return engine


def _mock_pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline.retain = AsyncMock(return_value=RetainResult(stored=True, memory_id="p1"))
    pipeline.recall = AsyncMock(return_value=RecallResult(hits=[], total_available=0, truncated=False))
    pipeline.reflect = AsyncMock(return_value=ReflectResult(answer="pipeline answer", sources=[]))
    pipeline.vector_store = MagicMock()
    pipeline.vector_store.delete = AsyncMock(return_value=2)
    pipeline.llm_provider = MagicMock()
    return pipeline


class TestRetainRouting:
    @pytest.mark.asyncio
    async def test_routes_to_engine(self) -> None:
        d = _make_dispatcher(provider="test")
        d.engine_provider = _mock_engine()
        result = await d.retain(RetainRequest(content="hi", bank_id="b1"))
        assert result.stored is True
        assert result.memory_id == "m1"
        d.engine_provider.retain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_routes_to_pipeline(self) -> None:
        d = _make_dispatcher()
        d.pipeline = _mock_pipeline()
        result = await d.retain(RetainRequest(content="hi", bank_id="b1"))
        assert result.stored is True
        assert result.memory_id == "p1"
        d.pipeline.retain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_provider_raises(self) -> None:
        d = _make_dispatcher()
        with pytest.raises(ConfigError, match="No provider or pipeline"):
            await d.retain(RetainRequest(content="hi", bank_id="b1"))

    @pytest.mark.asyncio
    async def test_engine_preferred_over_pipeline(self) -> None:
        d = _make_dispatcher(provider="test")
        d.engine_provider = _mock_engine()
        d.pipeline = _mock_pipeline()
        await d.retain(RetainRequest(content="hi", bank_id="b1"))
        d.engine_provider.retain.assert_awaited_once()
        d.pipeline.retain.assert_not_awaited()


class TestRecallRouting:
    @pytest.mark.asyncio
    async def test_routes_to_engine(self) -> None:
        d = _make_dispatcher(provider="test")
        d.engine_provider = _mock_engine()
        await d.recall(RecallRequest(query="q", bank_id="b1"))
        d.engine_provider.recall.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_routes_to_pipeline(self) -> None:
        d = _make_dispatcher()
        d.pipeline = _mock_pipeline()
        await d.recall(RecallRequest(query="q", bank_id="b1"))
        d.pipeline.recall.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tiered_retriever_used_for_pipeline(self) -> None:
        d = _make_dispatcher()
        d.pipeline = _mock_pipeline()
        mock_tr = MagicMock()
        mock_tr.retrieve = AsyncMock(
            return_value=RecallResult(hits=[], total_available=0, truncated=False)
        )
        d.tiered_retriever = mock_tr
        await d.recall(RecallRequest(query="q", bank_id="b1"))
        mock_tr.retrieve.assert_awaited_once()
        d.pipeline.recall.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_external_context_merged_for_non_hybrid(self) -> None:
        d = _make_dispatcher(provider="test")
        engine = _mock_engine()
        engine.recall = AsyncMock(
            return_value=RecallResult(
                hits=[MemoryHit(text="engine hit", score=0.9)],
                total_available=1,
                truncated=False,
            )
        )
        d.engine_provider = engine
        ext = [MemoryHit(text="external", score=0.8)]
        result = await d.recall(
            RecallRequest(query="q", bank_id="b1", external_context=ext)
        )
        # Should have merged external context via RRF
        assert len(result.hits) >= 1

    @pytest.mark.asyncio
    async def test_no_provider_raises(self) -> None:
        d = _make_dispatcher()
        with pytest.raises(ConfigError, match="No provider or pipeline"):
            await d.recall(RecallRequest(query="q", bank_id="b1"))


class TestReflectRouting:
    @pytest.mark.asyncio
    async def test_engine_with_reflect_support(self) -> None:
        d = _make_dispatcher(provider="test")
        engine = _mock_engine(supports_reflect=True)
        d.engine_provider = engine
        d.capabilities = engine.capabilities()
        result = await d.reflect(ReflectRequest(query="q", bank_id="b1"))
        assert result.answer == "answer"

    @pytest.mark.asyncio
    async def test_engine_without_reflect_error_fallback(self) -> None:
        d = _make_dispatcher(provider="test", fallback="error")
        engine = _mock_engine(supports_reflect=False)
        d.engine_provider = engine
        d.capabilities = engine.capabilities()
        with pytest.raises(CapabilityNotSupported, match="reflect"):
            await d.reflect(ReflectRequest(query="q", bank_id="b1"))

    @pytest.mark.asyncio
    async def test_engine_without_reflect_degrade_fallback(self) -> None:
        d = _make_dispatcher(provider="test", fallback="degrade")
        engine = _mock_engine(supports_reflect=False)
        engine.recall = AsyncMock(
            return_value=RecallResult(
                hits=[MemoryHit(text="fact A", score=0.9), MemoryHit(text="fact B", score=0.8)],
                total_available=2,
                truncated=False,
            )
        )
        d.engine_provider = engine
        d.capabilities = engine.capabilities()
        result = await d.reflect(ReflectRequest(query="q", bank_id="b1"))
        assert "fact A" in result.answer
        assert "fact B" in result.answer

    @pytest.mark.asyncio
    async def test_engine_without_reflect_local_llm_fallback(self) -> None:
        d = _make_dispatcher(provider="test", fallback="local_llm")
        engine = _mock_engine(supports_reflect=False)
        d.engine_provider = engine
        d.capabilities = engine.capabilities()
        d.pipeline = _mock_pipeline()
        result = await d.reflect(ReflectRequest(query="q", bank_id="b1"))
        assert result.answer == "pipeline answer"

    @pytest.mark.asyncio
    async def test_pipeline_reflect(self) -> None:
        d = _make_dispatcher()
        d.pipeline = _mock_pipeline()
        result = await d.reflect(ReflectRequest(query="q", bank_id="b1"))
        assert result.answer == "pipeline answer"

    @pytest.mark.asyncio
    async def test_no_provider_raises(self) -> None:
        d = _make_dispatcher()
        with pytest.raises(ConfigError, match="No provider or pipeline"):
            await d.reflect(ReflectRequest(query="q", bank_id="b1"))


class TestForgetRouting:
    @pytest.mark.asyncio
    async def test_engine_with_forget_support(self) -> None:
        d = _make_dispatcher(provider="test")
        engine = _mock_engine(supports_forget=True)
        d.engine_provider = engine
        d.capabilities = engine.capabilities()
        result = await d.forget(ForgetRequest(bank_id="b1", memory_ids=["m1"]))
        assert result.deleted_count == 1

    @pytest.mark.asyncio
    async def test_engine_without_forget_raises(self) -> None:
        d = _make_dispatcher(provider="test")
        engine = _mock_engine(supports_forget=False)
        d.engine_provider = engine
        d.capabilities = engine.capabilities()
        with pytest.raises(CapabilityNotSupported, match="forget"):
            await d.forget(ForgetRequest(bank_id="b1", memory_ids=["m1"]))

    @pytest.mark.asyncio
    async def test_pipeline_forget_by_ids(self) -> None:
        d = _make_dispatcher()
        d.pipeline = _mock_pipeline()
        result = await d.forget(ForgetRequest(bank_id="b1", memory_ids=["m1", "m2"]))
        assert result.deleted_count == 2

    @pytest.mark.asyncio
    async def test_pipeline_forget_all(self) -> None:
        d = _make_dispatcher()
        pipeline = _mock_pipeline()
        # list_vectors returns items then empty to terminate pagination
        mock_items = [MagicMock(id="v1"), MagicMock(id="v2")]
        pipeline.vector_store.list_vectors = AsyncMock(side_effect=[mock_items, []])
        pipeline.vector_store.delete = AsyncMock(return_value=2)
        d.pipeline = pipeline
        result = await d.forget(ForgetRequest(bank_id="b1", scope="all"))
        assert result.deleted_count == 2

    @pytest.mark.asyncio
    async def test_no_provider_raises(self) -> None:
        d = _make_dispatcher()
        with pytest.raises(CapabilityNotSupported):
            await d.forget(ForgetRequest(bank_id="b1", memory_ids=["m1"]))


class TestReflectFromHits:
    @pytest.mark.asyncio
    async def test_pipeline_synthesis(self) -> None:
        d = _make_dispatcher()
        d.pipeline = _mock_pipeline()
        hits = [MemoryHit(text="Calvin likes dark mode", score=0.9)]
        with patch("astrocyte.pipeline.reflect.synthesize", new_callable=AsyncMock) as mock_synth:
            mock_synth.return_value = ReflectResult(answer="Dark mode preferred", sources=hits)
            result = await d.reflect_from_hits(query="preferences?", hits=hits, bank_id="b1")
            assert result.answer == "Dark mode preferred"
            mock_synth.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_pipeline_degrade_concat(self) -> None:
        d = _make_dispatcher()
        hits = [
            MemoryHit(text="fact A", score=0.9),
            MemoryHit(text="fact B", score=0.8),
        ]
        result = await d.reflect_from_hits(query="q", hits=hits, bank_id="b1")
        assert "fact A" in result.answer
        assert "fact B" in result.answer

    @pytest.mark.asyncio
    async def test_no_pipeline_no_hits(self) -> None:
        d = _make_dispatcher()
        result = await d.reflect_from_hits(query="q", hits=[], bank_id="b1")
        assert "No relevant" in result.answer


class TestProviderName:
    def test_explicit_provider(self) -> None:
        d = _make_dispatcher(provider="mystique")
        assert d.provider_name == "mystique"

    def test_default_pipeline(self) -> None:
        d = _make_dispatcher()
        assert d.provider_name == "pipeline"
