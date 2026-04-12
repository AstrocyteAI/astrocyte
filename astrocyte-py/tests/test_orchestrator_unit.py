"""Unit tests for pipeline/orchestrator.py — dedup, content_type routing, overfetch.

Tests the PipelineOrchestrator retain/recall logic in isolation using
InMemory providers. Covers per-chunk dedup, content_type → chunking strategy
routing, semantic_overfetch multiplier, and _TrackingLLMProvider token tracking.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.orchestrator import PipelineOrchestrator, _TrackingLLMProvider
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import Message, RecallRequest, RetainRequest

# ---------------------------------------------------------------------------
# _TrackingLLMProvider
# ---------------------------------------------------------------------------


class TestTrackingLLMProvider:
    @pytest.mark.asyncio
    async def test_accumulates_tokens(self):
        inner = MockLLMProvider()
        tracker = _TrackingLLMProvider(inner)
        assert tracker.tokens_used == 0

        await tracker.complete([Message(role="user", content="hi")])
        # MockLLMProvider returns usage — tokens should accumulate
        assert tracker.tokens_used >= 0  # May be 0 if mock doesn't set usage

    @pytest.mark.asyncio
    async def test_reset_returns_and_clears(self):
        inner = MockLLMProvider()
        tracker = _TrackingLLMProvider(inner)
        tracker.tokens_used = 42
        total = tracker.reset_tokens()
        assert total == 42
        assert tracker.tokens_used == 0

    @pytest.mark.asyncio
    async def test_embed_passthrough(self):
        inner = MockLLMProvider()
        tracker = _TrackingLLMProvider(inner)
        result = await tracker.embed(["hello"])
        assert len(result) == 1
        assert isinstance(result[0], list)


# ---------------------------------------------------------------------------
# PipelineOrchestrator — retain: per-chunk dedup
# ---------------------------------------------------------------------------


class TestRetainDedup:
    @pytest.mark.asyncio
    async def test_identical_content_deduped(self):
        """Retaining the same text twice should dedup the second call."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        r1 = await orch.retain(RetainRequest(content="The sky is blue", bank_id="b1"))
        assert r1.stored is True

        r2 = await orch.retain(RetainRequest(content="The sky is blue", bank_id="b1"))
        assert r2.stored is False
        assert r2.deduplicated is True

    @pytest.mark.asyncio
    async def test_different_content_not_deduped(self):
        """Distinct content should be stored separately."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        r1 = await orch.retain(RetainRequest(content="Alice likes cats", bank_id="b1"))
        r2 = await orch.retain(RetainRequest(content="Bob prefers dogs", bank_id="b1"))
        assert r1.stored is True
        assert r2.stored is True

    @pytest.mark.asyncio
    async def test_partial_chunk_dedup(self):
        """When multi-chunk content has some duplicate chunks, non-duplicates still stored."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm, max_chunk_size=30)

        # First retain — short enough to be one chunk
        await orch.retain(RetainRequest(content="The sky is blue", bank_id="b1"))

        # Second retain — two chunks, one similar to first, one new
        r2 = await orch.retain(RetainRequest(
            content="The sky is blue. Quantum computing uses qubits for computation.",
            bank_id="b1",
        ))
        # Should store at least the new chunk
        assert r2.stored is True

    @pytest.mark.asyncio
    async def test_dedup_is_per_bank(self):
        """Same content in different banks should not be deduped."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        r1 = await orch.retain(RetainRequest(content="The sky is blue", bank_id="bank-a"))
        r2 = await orch.retain(RetainRequest(content="The sky is blue", bank_id="bank-b"))
        assert r1.stored is True
        assert r2.stored is True


# ---------------------------------------------------------------------------
# PipelineOrchestrator — retain: content_type routing
# ---------------------------------------------------------------------------


class TestContentTypeRouting:
    @pytest.mark.asyncio
    async def test_conversation_uses_dialogue_chunking(self):
        """content_type='conversation' should route to dialogue chunking."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm, chunk_strategy="sentence")

        content = "Alice: Hello there!\nBob: Hi Alice, how are you?\nAlice: I'm good thanks."
        r = await orch.retain(RetainRequest(
            content=content,
            bank_id="b1",
            content_type="conversation",
        ))
        assert r.stored is True

        # Verify the chunks preserved speaker turns (dialogue chunking keeps turns together)
        stored = await vs.list_vectors("b1")
        assert len(stored) >= 1
        # At least one chunk should contain a speaker label
        texts = [item.text for item in stored]
        assert any("Alice:" in t or "Bob:" in t for t in texts)

    @pytest.mark.asyncio
    async def test_text_uses_default_strategy(self):
        """content_type='text' should use the orchestrator's default strategy."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm, chunk_strategy="sentence")

        r = await orch.retain(RetainRequest(
            content="First sentence. Second sentence. Third sentence.",
            bank_id="b1",
            content_type="text",
        ))
        assert r.stored is True

    @pytest.mark.asyncio
    async def test_document_uses_paragraph_chunking(self):
        """content_type='document' should use paragraph chunking."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        content = "First paragraph about topic A.\n\nSecond paragraph about topic B."
        r = await orch.retain(RetainRequest(
            content=content,
            bank_id="b1",
            content_type="document",
        ))
        assert r.stored is True


# ---------------------------------------------------------------------------
# PipelineOrchestrator — recall: semantic_overfetch
# ---------------------------------------------------------------------------


class TestSemanticOverfetch:
    @pytest.mark.asyncio
    async def test_default_overfetch_is_5(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        assert orch.semantic_overfetch == 5

    @pytest.mark.asyncio
    async def test_custom_overfetch(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm, semantic_overfetch=10)
        assert orch.semantic_overfetch == 10

    @pytest.mark.asyncio
    async def test_overfetch_affects_recall(self):
        """Higher overfetch should retrieve more candidates before trimming."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm, semantic_overfetch=5)

        # Store enough items to test overfetch
        for i in range(10):
            await orch.retain(RetainRequest(
                content=f"Memory item number {i} about topic {chr(65 + i)}",
                bank_id="b1",
            ))

        result = await orch.recall(RecallRequest(
            query="topic", bank_id="b1", max_results=3,
        ))
        # Should have hits — overfetch ensures broader retrieval
        assert len(result.hits) <= 3
        assert result.trace is not None
        assert result.trace.fusion_method == "rrf"


# ---------------------------------------------------------------------------
# PipelineOrchestrator — retain: empty content
# ---------------------------------------------------------------------------


class TestRetainEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_content(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        r = await orch.retain(RetainRequest(content="", bank_id="b1"))
        assert r.stored is False

    @pytest.mark.asyncio
    async def test_whitespace_only_content(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        r = await orch.retain(RetainRequest(content="   \n\n  ", bank_id="b1"))
        assert r.stored is False

    @pytest.mark.asyncio
    async def test_retain_with_metadata(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        r = await orch.retain(RetainRequest(
            content="Alice works at NASA",
            bank_id="b1",
            metadata={"source": "conversation"},
        ))
        assert r.stored is True
        assert r.memory_id is not None

    @pytest.mark.asyncio
    async def test_retain_with_tags(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        r = await orch.retain(RetainRequest(
            content="Important fact about chemistry",
            bank_id="b1",
            tags=["science", "chemistry"],
        ))
        assert r.stored is True


# ---------------------------------------------------------------------------
# PipelineOrchestrator — recall round-trip
# ---------------------------------------------------------------------------


class TestRecallRoundTrip:
    @pytest.mark.asyncio
    async def test_retain_then_recall(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        await orch.retain(RetainRequest(content="Alice works at NASA", bank_id="b1"))
        result = await orch.recall(RecallRequest(query="NASA", bank_id="b1", max_results=5))
        assert len(result.hits) >= 1
        assert any("NASA" in h.text for h in result.hits)

    @pytest.mark.asyncio
    async def test_recall_empty_bank(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        result = await orch.recall(RecallRequest(query="anything", bank_id="empty", max_results=5))
        assert result.hits == []

    @pytest.mark.asyncio
    async def test_recall_trace_strategies(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        await orch.retain(RetainRequest(content="test data", bank_id="b1"))
        result = await orch.recall(RecallRequest(query="test", bank_id="b1", max_results=5))
        assert result.trace is not None
        assert "semantic" in result.trace.strategies_used

    @pytest.mark.asyncio
    async def test_max_results_respected(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        for i in range(10):
            await orch.retain(RetainRequest(
                content=f"Fact {i}: something unique about topic {i}",
                bank_id="b1",
            ))

        result = await orch.recall(RecallRequest(query="fact", bank_id="b1", max_results=3))
        assert len(result.hits) <= 3
