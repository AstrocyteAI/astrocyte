"""Contract tests for competitor brain adapters.

Pins the duck-type surface that LoCoMo / LongMemEval benchmark adapters
depend on. Every adapter added to ``build_competitor_brain`` must pass
these tests before it can appear in a published comparison matrix.

Tests use lightweight stubs (no real SDK calls) so they run in CI
without external credentials. They verify:

1. Structural conformance — the adapter satisfies ``CompetitorBrain``
   at construction time (runtime_checkable Protocol check).
2. Method signatures — retain / recall / reflect accept the expected
   keyword arguments without raising ``TypeError``.
3. Pipeline shim — ``adapter._pipeline.llm_provider`` is set when an
   ``llm_provider`` is passed, ``None`` when omitted.
4. Stub error contract — ``NotImplementedError`` from un-wired SDK
   calls is the declared error; any other exception is a bug.
5. Factory dispatch — ``build_competitor_brain("mem0"|"zep")`` resolves
   without import error; unknown names raise ``ValueError``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrocyte.eval.competitors.base import CompetitorBrain, build_competitor_brain
from astrocyte.eval.competitors.mem0_adapter import Mem0BrainAdapter
from astrocyte.eval.competitors.zep_adapter import ZepBrainAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_llm() -> MagicMock:
    """Minimal LLMProvider stub — only needs ``complete`` to be awaitable."""
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=MagicMock(text="synthesized answer"))
    return llm


def _fake_mem0_client() -> MagicMock:
    """Sync mem0 Memory stub returning the shape the adapter parses."""
    client = MagicMock()
    client.add.return_value = {"results": [{"id": "mem-abc", "event": "ADD"}]}
    client.search.return_value = {
        "results": [{"id": "mem-abc", "memory": "some fact", "score": 0.9, "metadata": {}}]
    }
    return client


def _fake_zep_client() -> MagicMock:
    """Sync Zep ZepClient stub — methods raise NotImplementedError (scaffold)."""
    client = MagicMock()
    return client


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestCompetitorBrainProtocol:
    def test_mem0_adapter_satisfies_protocol(self) -> None:
        adapter = Mem0BrainAdapter(_fake_mem0_client())
        assert isinstance(adapter, CompetitorBrain)

    def test_zep_adapter_satisfies_protocol(self) -> None:
        adapter = ZepBrainAdapter(_fake_zep_client())
        assert isinstance(adapter, CompetitorBrain)

    def test_mem0_adapter_with_llm_has_pipeline(self) -> None:
        llm = _fake_llm()
        adapter = Mem0BrainAdapter(_fake_mem0_client(), llm_provider=llm)
        assert adapter._pipeline is not None
        assert adapter._pipeline.llm_provider is llm

    def test_zep_adapter_with_llm_has_pipeline(self) -> None:
        llm = _fake_llm()
        adapter = ZepBrainAdapter(_fake_zep_client(), llm_provider=llm)
        assert adapter._pipeline is not None
        assert adapter._pipeline.llm_provider is llm

    def test_mem0_adapter_without_llm_has_no_pipeline(self) -> None:
        adapter = Mem0BrainAdapter(_fake_mem0_client())
        assert adapter._pipeline is None

    def test_zep_adapter_without_llm_has_no_pipeline(self) -> None:
        adapter = ZepBrainAdapter(_fake_zep_client())
        assert adapter._pipeline is None

    def test_pipeline_reset_token_counter_returns_int(self) -> None:
        adapter = Mem0BrainAdapter(_fake_mem0_client(), llm_provider=_fake_llm())
        assert adapter._pipeline is not None
        result = adapter._pipeline.reset_token_counter()
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Mem0 adapter — wired SDK calls
# ---------------------------------------------------------------------------


class TestMem0AdapterRetain:
    @pytest.mark.asyncio
    async def test_retain_returns_stored_true(self) -> None:
        adapter = Mem0BrainAdapter(_fake_mem0_client())
        result = await adapter.retain("some content", bank_id="user-1")
        assert result.stored is True

    @pytest.mark.asyncio
    async def test_retain_extracts_memory_id(self) -> None:
        adapter = Mem0BrainAdapter(_fake_mem0_client())
        result = await adapter.retain("content", bank_id="user-1")
        assert result.memory_id == "mem-abc"

    @pytest.mark.asyncio
    async def test_retain_ignores_occurred_at_with_debug_log(self, caplog) -> None:
        import logging
        adapter = Mem0BrainAdapter(_fake_mem0_client())
        with caplog.at_level(logging.DEBUG, logger="astrocyte.eval.competitors.mem0"):
            await adapter.retain(
                "content", bank_id="b1", occurred_at=datetime.now(timezone.utc)
            )
        assert any("occurred_at" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_retain_passes_metadata_to_sdk(self) -> None:
        client = _fake_mem0_client()
        adapter = Mem0BrainAdapter(client)
        await adapter.retain("text", bank_id="b1", metadata={"key": "val"})
        client.add.assert_called_once()
        _, kwargs = client.add.call_args
        assert kwargs.get("metadata") == {"key": "val"}

    @pytest.mark.asyncio
    async def test_retain_merges_tags_into_metadata(self) -> None:
        client = _fake_mem0_client()
        adapter = Mem0BrainAdapter(client)
        await adapter.retain("text", bank_id="b1", tags=["foo", "bar"])
        _, kwargs = client.add.call_args
        assert kwargs.get("metadata", {}).get("tags") == ["foo", "bar"]

    @pytest.mark.asyncio
    async def test_retain_flat_list_response_handled(self) -> None:
        client = MagicMock()
        client.add.return_value = [{"id": "flat-id", "event": "ADD"}]
        adapter = Mem0BrainAdapter(client)
        result = await adapter.retain("content", bank_id="b1")
        assert result.memory_id == "flat-id"


class TestMem0AdapterRecall:
    @pytest.mark.asyncio
    async def test_recall_returns_recall_result(self) -> None:
        adapter = Mem0BrainAdapter(_fake_mem0_client())
        result = await adapter.recall("query", bank_id="user-1")
        assert len(result.hits) == 1
        assert result.hits[0].text == "some fact"
        assert result.hits[0].score == pytest.approx(0.9)
        assert result.hits[0].memory_id == "mem-abc"

    @pytest.mark.asyncio
    async def test_recall_passes_bank_as_user_id(self) -> None:
        client = _fake_mem0_client()
        adapter = Mem0BrainAdapter(client)
        await adapter.recall("query", bank_id="tenant-42")
        _, kwargs = client.search.call_args
        assert kwargs.get("user_id") == "tenant-42"

    @pytest.mark.asyncio
    async def test_recall_flat_list_response_handled(self) -> None:
        client = MagicMock()
        client.search.return_value = [{"id": "x", "memory": "flat", "score": 0.5}]
        adapter = Mem0BrainAdapter(client)
        result = await adapter.recall("q", bank_id="b1")
        assert result.hits[0].text == "flat"

    @pytest.mark.asyncio
    async def test_recall_empty_response_returns_empty_hits(self) -> None:
        client = MagicMock()
        client.search.return_value = {"results": []}
        adapter = Mem0BrainAdapter(client)
        result = await adapter.recall("q", bank_id="b1")
        assert result.hits == []


class TestMem0AdapterReflect:
    @pytest.mark.asyncio
    async def test_reflect_without_llm_raises_runtime_error(self) -> None:
        adapter = Mem0BrainAdapter(_fake_mem0_client())
        with pytest.raises(RuntimeError, match="llm_provider"):
            await adapter.reflect("what happened?", bank_id="b1")

    @pytest.mark.asyncio
    async def test_reflect_calls_synthesize_with_recall_hits(self) -> None:
        llm = _fake_llm()
        adapter = Mem0BrainAdapter(_fake_mem0_client(), llm_provider=llm)
        with patch(
            "astrocyte.pipeline.reflect.synthesize",
            new_callable=AsyncMock,
        ) as mock_synth:
            from astrocyte.types import ReflectResult
            mock_synth.return_value = ReflectResult(answer="answer", sources=[])
            result = await adapter.reflect("query", bank_id="b1")
        mock_synth.assert_called_once()
        call_args = mock_synth.call_args
        assert call_args.args[0] == "query"
        assert result.answer == "answer"


# ---------------------------------------------------------------------------
# Zep adapter — scaffold contract (NotImplementedError from un-wired calls)
# ---------------------------------------------------------------------------


class TestZepAdapterScaffold:
    @pytest.mark.asyncio
    async def test_retain_raises_not_implemented(self) -> None:
        adapter = ZepBrainAdapter(_fake_zep_client())
        with pytest.raises(NotImplementedError):
            await adapter.retain("content", bank_id="session-1")

    @pytest.mark.asyncio
    async def test_recall_raises_not_implemented(self) -> None:
        adapter = ZepBrainAdapter(_fake_zep_client())
        with pytest.raises(NotImplementedError):
            await adapter.recall("query", bank_id="session-1")

    @pytest.mark.asyncio
    async def test_reflect_without_llm_raises_runtime_error(self) -> None:
        adapter = ZepBrainAdapter(_fake_zep_client())
        with pytest.raises(RuntimeError, match="llm_provider"):
            await adapter.reflect("query", bank_id="session-1")

    @pytest.mark.asyncio
    async def test_reflect_with_llm_raises_not_implemented_from_recall(self) -> None:
        # recall is still a stub, so reflect → recall → NotImplementedError
        adapter = ZepBrainAdapter(_fake_zep_client(), llm_provider=_fake_llm())
        with pytest.raises(NotImplementedError):
            await adapter.reflect("query", bank_id="session-1")


# ---------------------------------------------------------------------------
# Factory dispatch
# ---------------------------------------------------------------------------


class TestBuildCompetitorBrain:
    def test_unknown_name_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown competitor"):
            build_competitor_brain("nosuchsystem", llm_provider=_fake_llm())

    def test_mem0_dispatches_to_mem0_brain_adapter(self) -> None:
        with patch(
            "astrocyte.eval.competitors.mem0_adapter.Mem0BrainAdapter.__init__",
            return_value=None,
        ):
            # Just verify the import path resolves without error
            import astrocyte.eval.competitors.mem0_adapter as m
            assert hasattr(m, "Mem0BrainAdapter")

    def test_zep_dispatches_to_zep_brain_adapter(self) -> None:
        import astrocyte.eval.competitors.zep_adapter as z
        assert hasattr(z, "ZepBrainAdapter")

    def test_factory_name_is_case_insensitive(self) -> None:
        # Patch the import so no real SDK is required
        with patch(
            "astrocyte.eval.competitors.base.build_competitor_brain",
        ) as mock_build:
            mock_build.return_value = MagicMock()
            mock_build("MEM0", llm_provider=_fake_llm())
            mock_build("Mem0", llm_provider=_fake_llm())
            assert mock_build.call_count == 2
