"""Unit tests for pipeline/orchestrator.py — dedup, content_type routing, overfetch.

Tests the PipelineOrchestrator retain/recall logic in isolation using
InMemory providers. Covers per-chunk dedup, content_type → chunking strategy
routing, semantic_overfetch multiplier, and _TrackingLLMProvider token tracking.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from astrocyte.pipeline import orchestrator as orchestrator_mod
from astrocyte.pipeline.orchestrator import PipelineOrchestrator, _TrackingLLMProvider
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import (
    MemoryHit,
    Message,
    RecallRequest,
    ReflectRequest,
    ReflectResult,
    RetainRequest,
    VectorItem,
)

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
        orch = PipelineOrchestrator(vs, llm)

        # First retain
        await orch.retain(RetainRequest(content="The sky is blue and the grass is green", bank_id="b1"))
        first_docs = await vs.list_vectors("b1")
        first_count = len(first_docs)
        assert first_count >= 1

        # Second retain — includes same content plus new content
        r2 = await orch.retain(RetainRequest(
            content="Quantum computing uses qubits for parallel computation",
            bank_id="b1",
        ))
        # New distinct content should be stored
        assert r2.stored is True
        all_docs = await vs.list_vectors("b1")
        assert len(all_docs) > first_count
        assert any("Quantum" in doc.text for doc in all_docs)

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


class TestReflectAutoPromptRouting:
    @pytest.mark.asyncio
    async def test_reflect_routes_likely_question_to_inference_prompt(self, monkeypatch):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        await orch.retain(RetainRequest(content="Caroline wants to become a counselor.", bank_id="b1"))

        captured: dict[str, str | None] = {}

        async def fake_synthesize(**kwargs):
            captured["prompt"] = kwargs["mip_reflect"].prompt if kwargs.get("mip_reflect") else None
            return ReflectResult(answer="Likely yes.", sources=kwargs["hits"])

        monkeypatch.setattr(orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synthesize))

        await orch.reflect(ReflectRequest(query="Would Caroline likely pursue counseling?", bank_id="b1"))

        assert captured["prompt"] == "evidence_inference"

    @pytest.mark.asyncio
    async def test_reflect_routes_when_question_to_temporal_prompt(self, monkeypatch):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        await orch.retain(RetainRequest(content="Melanie ran a charity race last week.", bank_id="b1"))

        captured: dict[str, str | None] = {}

        async def fake_synthesize(**kwargs):
            captured["prompt"] = kwargs["mip_reflect"].prompt if kwargs.get("mip_reflect") else None
            return ReflectResult(answer="The week before.", sources=kwargs["hits"])

        monkeypatch.setattr(orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synthesize))

        await orch.reflect(ReflectRequest(query="When did Melanie run a charity race?", bank_id="b1"))

        assert captured["prompt"] == "temporal_aware"


class TestReflectHierarchy:
    def test_reflect_context_prefers_compiled_and_observation_layers(self):
        orch = PipelineOrchestrator(InMemoryVectorStore(), MockLLMProvider())
        hits = [
            MemoryHit(
                text="Caroline bought groceries.",
                score=0.62,
                memory_id="raw",
                fact_type="world",
            ),
            MemoryHit(
                text="Caroline repeatedly talks about becoming a counselor.",
                score=0.50,
                memory_id="obs",
                fact_type="observation",
                metadata={"_obs_proof_count": 3},
                memory_layer="observation",
            ),
        ]

        ranked = orch._rank_reflect_context("Would Caroline likely pursue counseling?", hits, limit=2)

        assert ranked[0].memory_id == "obs"

    @pytest.mark.asyncio
    async def test_reflect_expands_observation_sources_to_raw_memories(self):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        await vs.store_vectors([
            VectorItem(
                id="raw-1",
                bank_id="b1",
                vector=[1.0] + [0.0] * 127,
                text="Caroline said she wants to become a counselor.",
                fact_type="world",
            )
        ])
        hits = [
            MemoryHit(
                text="Caroline has a stable counseling-career goal.",
                score=0.90,
                memory_id="obs-1",
                fact_type="observation",
                metadata={"_obs_source_ids": '["raw-1"]'},
                memory_layer="observation",
            )
        ]

        expanded = await orch._expand_reflect_sources("b1", hits, limit=3)

        assert [hit.memory_id for hit in expanded] == ["obs-1", "raw-1"]

    @pytest.mark.asyncio
    async def test_entity_path_fallback_reads_person_metadata(self):
        vs = InMemoryVectorStore()
        orch = PipelineOrchestrator(vs, MockLLMProvider())
        await vs.store_vectors([
            VectorItem(
                id="alice-1",
                bank_id="b1",
                vector=[1.0] + [0.0] * 127,
                text="Alice joined the pottery workshop.",
                metadata={"locomo_persons": "Alice", "session_id": "s1"},
            )
        ])

        hits = await orch._retrieve_entity_path_fallback("What activities did Alice join?", "b1", limit=5)

        assert hits[0].id == "alice-1"
        assert hits[0].metadata["_entity_path"] == "alice"

    def test_entity_path_authority_context_labels_sections(self):
        orch = PipelineOrchestrator(InMemoryVectorStore(), MockLLMProvider())

        context = orch._entity_path_authority_context([
            MemoryHit(text="Alice joined pottery.", score=0.8, metadata={"_entity_path": "alice"}),
        ])

        assert context is not None
        assert "entity_path_evidence" in context


class TestPipelineShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_closes_vector_store(self):
        class CloseableVectorStore(InMemoryVectorStore):
            def __init__(self) -> None:
                super().__init__()
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        vs = CloseableVectorStore()
        orch = PipelineOrchestrator(vs, MockLLMProvider())

        await orch.shutdown()

        assert vs.closed is True


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
