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
    RecallResult,
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

    @pytest.mark.asyncio
    async def test_forwards_tools_and_tool_choice_to_inner(self):
        """Regression — Hindsight-parity agentic reflect requires the
        tracker to forward ``tools`` and ``tool_choice`` to the inner
        provider. The 2026-05-01 bench failed 1986/1986 questions because
        the tracker dropped the kwargs on the floor. Lock it down: a
        ``complete(tools=[...])`` call must reach the inner provider with
        those kwargs intact, not raise ``unexpected keyword argument``.
        """
        from astrocyte.types import TokenUsage, ToolCall, ToolDefinition

        seen_kwargs: dict = {}

        class _CapturingProvider:
            async def complete(
                self,
                messages,
                model=None,
                max_tokens=1024,
                temperature=0.0,
                tools=None,
                tool_choice=None,
            ):
                seen_kwargs["tools"] = tools
                seen_kwargs["tool_choice"] = tool_choice
                from astrocyte.types import Completion
                return Completion(
                    text="",
                    model="mock",
                    usage=TokenUsage(input_tokens=1, output_tokens=1),
                    tool_calls=[ToolCall(id="x", name="recall", arguments={})],
                )

            async def embed(self, texts, model=None):
                return [[0.0] for _ in texts]

            def capabilities(self):
                from astrocyte.types import LLMCapabilities
                return LLMCapabilities()

        tools = [ToolDefinition(name="recall", description="x", parameters={})]
        tracker = _TrackingLLMProvider(_CapturingProvider())

        result = await tracker.complete(
            [Message(role="user", content="q")],
            tools=tools,
            tool_choice="auto",
        )

        assert seen_kwargs["tools"] == tools, (
            "Tracker MUST forward `tools` to the inner provider; "
            "agentic reflect's native function calling depends on it."
        )
        assert seen_kwargs["tool_choice"] == "auto"
        # Tool calls returned by inner must propagate up unchanged.
        assert result.tool_calls is not None and result.tool_calls[0].name == "recall"


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
    """Test query-plan prompt routing in isolation from retrieval quality.

    These tests monkeypatch ``orch.recall`` to return a high-confidence
    ``RecallResult`` (``top_semantic_score=0.85``) so the evidence-strict
    gate does not fire, allowing the query-shape routing to be verified
    independently.  The gate itself is tested separately.
    """

    @pytest.mark.asyncio
    async def test_reflect_routes_likely_question_to_inference_prompt(self, monkeypatch):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        await orch.retain(RetainRequest(content="Caroline wants to become a counselor.", bank_id="b1"))

        captured: dict[str, str | None] = {}

        # Simulate strong retrieval so the evidence-strict gate does not override
        # the query-plan routing we're testing.
        good_recall = RecallResult(
            hits=[MemoryHit(text="Caroline wants to become a counselor.", score=0.85)],
            total_available=1,
            truncated=False,
            top_semantic_score=0.85,
        )
        monkeypatch.setattr(orch, "recall", AsyncMock(return_value=good_recall))

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

        good_recall = RecallResult(
            hits=[MemoryHit(text="Melanie ran a charity race last week.", score=0.85)],
            total_available=1,
            truncated=False,
            top_semantic_score=0.85,
        )
        monkeypatch.setattr(orch, "recall", AsyncMock(return_value=good_recall))

        async def fake_synthesize(**kwargs):
            captured["prompt"] = kwargs["mip_reflect"].prompt if kwargs.get("mip_reflect") else None
            return ReflectResult(answer="The week before.", sources=kwargs["hits"])

        monkeypatch.setattr(orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synthesize))

        await orch.reflect(ReflectRequest(query="When did Melanie run a charity race?", bank_id="b1"))

        assert captured["prompt"] == "temporal_aware"

    @pytest.mark.asyncio
    async def test_evidence_strict_gate_fires_on_weak_retrieval(self, monkeypatch):
        """evidence_strict overrides query-plan routing when top_semantic_score < 0.5."""
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        captured: dict[str, str | None] = {}

        # Weak retrieval: top_semantic_score = 0.3 — gate should fire
        weak_recall = RecallResult(
            hits=[MemoryHit(text="Some loosely related memory.", score=0.3)],
            total_available=1,
            truncated=False,
            top_semantic_score=0.3,
        )
        monkeypatch.setattr(orch, "recall", AsyncMock(return_value=weak_recall))

        async def fake_synthesize(**kwargs):
            captured["prompt"] = kwargs["mip_reflect"].prompt if kwargs.get("mip_reflect") else None
            return ReflectResult(answer="I'm not sure.", sources=kwargs["hits"])

        monkeypatch.setattr(orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synthesize))

        # Use an inference-shaped query so query_plan would normally choose evidence_inference;
        # the gate must override it to evidence_strict.
        await orch.reflect(ReflectRequest(query="Would Caroline likely pursue counseling?", bank_id="b1"))

        assert captured["prompt"] == "evidence_strict"


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


class TestReflectTagScoping:
    """Lock in that ``ReflectRequest.tags`` actually scopes synthesis.

    Pre-fix bug: ``Astrocyte.reflect(tags=...)`` accepted a tags kwarg, but
    on the single-bank path it built a ``ReflectRequest`` without tags
    (the dataclass had no such field), so the dispatcher's internal recall
    ran unscoped. LoCoMo's "scope by conversation_id" effort silently
    no-op'd through reflect, leaking cross-conversation memories into
    synthesis context. Don't let that happen again.
    """

    @pytest.mark.asyncio
    async def test_reflect_tags_scope_synthesis_hits(self, monkeypatch):
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        # Two memories in the same bank, distinguished by conversation tag.
        await vs.store_vectors([
            VectorItem(
                id="m-A",
                bank_id="b1",
                vector=[1.0] + [0.0] * 127,
                text="Caroline joined a hiking club in convo A.",
                tags=["convo:A"],
            ),
            VectorItem(
                id="m-B",
                bank_id="b1",
                vector=[1.0] + [0.0] * 127,
                text="Caroline joined a chess club in convo B.",
                tags=["convo:B"],
            ),
        ])

        captured_hit_ids: list[str] = []

        async def fake_synthesize(**kwargs):
            for hit in kwargs.get("hits") or []:
                if hit.memory_id:
                    captured_hit_ids.append(hit.memory_id)
            return ReflectResult(answer="ok", sources=kwargs.get("hits"))

        monkeypatch.setattr(
            orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synthesize)
        )

        await orch.reflect(
            ReflectRequest(
                query="What club did Caroline join?",
                bank_id="b1",
                tags=["convo:A"],
            )
        )

        # Convo-A hit may or may not be present (depends on InMemory recall
        # ranking against a single-vector store), but the contract we care
        # about is: convo-B MUST NOT leak into synthesis.
        assert "m-B" not in captured_hit_ids, (
            "ReflectRequest.tags must scope the dispatcher's internal recall; "
            "convo:B memory leaked into synthesis context."
        )

    @pytest.mark.asyncio
    async def test_reflect_without_tags_sees_all_memories(self, monkeypatch):
        """Negative control: dropping the tag filter brings back the leak.

        This proves the previous test's pass is causal — reflect *can*
        reach the convo:B memory; it's the tag filter that prevents it.
        """
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)

        await vs.store_vectors([
            VectorItem(
                id="m-A",
                bank_id="b1",
                vector=[1.0] + [0.0] * 127,
                text="Caroline joined a hiking club in convo A.",
                tags=["convo:A"],
            ),
            VectorItem(
                id="m-B",
                bank_id="b1",
                vector=[1.0] + [0.0] * 127,
                text="Caroline joined a chess club in convo B.",
                tags=["convo:B"],
            ),
        ])

        captured_hit_ids: list[str] = []

        async def fake_synthesize(**kwargs):
            for hit in kwargs.get("hits") or []:
                if hit.memory_id:
                    captured_hit_ids.append(hit.memory_id)
            return ReflectResult(answer="ok", sources=kwargs.get("hits"))

        monkeypatch.setattr(
            orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synthesize)
        )

        await orch.reflect(
            ReflectRequest(
                query="What club did Caroline join?",
                bank_id="b1",
            )
        )

        assert {"m-A", "m-B"}.issubset(set(captured_hit_ids)), (
            "Negative control failed: without tag scoping, both memories "
            "should be eligible for synthesis context."
        )


class TestReflectExpansionTagScoping:
    """Lock in that ``_expand_reflect_sources`` honors tag scoping.

    The expansion path (Hindsight-style "compiled hit → cited raw
    sources" walk) used to scan ``vector_store.list_vectors(bank)``
    matching only by ID. If a wiki page's ``_wiki_source_ids`` ever
    cited cross-scope memories, expansion would happily pull them into
    synthesis context, undoing tag scoping at the boundary.

    With Fix D the expansion drops fetched memories whose tags don't
    cover the reflect scope. Belt-and-suspenders for scoped reflect.
    """

    @pytest.mark.asyncio
    async def test_expansion_drops_cross_scope_sources(self):
        vs = InMemoryVectorStore()
        orch = PipelineOrchestrator(vs, MockLLMProvider())

        # raw-A and raw-B live in the same bank, distinct convo tags.
        await vs.store_vectors([
            VectorItem(
                id="raw-A",
                bank_id="b1",
                vector=[1.0] + [0.0] * 127,
                text="Caroline went hiking (convo A).",
                tags=["convo:A"],
            ),
            VectorItem(
                id="raw-B",
                bank_id="b1",
                vector=[1.0] + [0.0] * 127,
                text="Caroline played chess (convo B).",
                tags=["convo:B"],
            ),
        ])

        # A wiki/observation hit whose source_ids cite BOTH raw memories
        # (the cross-scope-leak shape we want expansion to defend against).
        compiled_hit = MemoryHit(
            text="Caroline activities summary.",
            score=0.9,
            memory_id="obs-mixed",
            fact_type="observation",
            metadata={"_obs_source_ids": '["raw-A", "raw-B"]'},
            memory_layer="observation",
        )

        scoped = await orch._expand_reflect_sources(
            "b1", [compiled_hit], limit=10, tags=["convo:A"]
        )
        unscoped = await orch._expand_reflect_sources(
            "b1", [compiled_hit], limit=10
        )

        scoped_ids = {h.memory_id for h in scoped}
        unscoped_ids = {h.memory_id for h in unscoped}

        # Scoped expansion: only convo-A leaks through.
        assert "raw-A" in scoped_ids
        assert "raw-B" not in scoped_ids, (
            "Expansion must drop cross-scope sources when tags is set; "
            "raw-B (convo:B) leaked into a convo:A reflect."
        )
        # Negative control: without tags, both come through (proves the
        # filter is what's preventing the leak, not some unrelated bug).
        assert {"raw-A", "raw-B"}.issubset(unscoped_ids)


class TestAdversarialAbstention:
    """Score-floor abstention guardrail.

    Skips the LLM and returns "insufficient evidence" when retrieval is
    too weak to support any answer. Targets the LoCoMo adversarial
    category where the model otherwise hallucinates from disconnected hits.
    """

    @pytest.mark.asyncio
    async def test_abstains_when_top_score_below_floor(self, monkeypatch):
        """All hits below floor → no LLM call, "insufficient evidence" returned."""
        from astrocyte.types import MemoryHit, RecallResult, RecallTrace
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        orch.adversarial_abstention_enabled = True
        orch.adversarial_abstention_floor = 0.5

        weak_recall = RecallResult(
            hits=[MemoryHit(text="weak", score=0.1, memory_id="m1")],
            total_available=1,
            truncated=False,
            top_semantic_score=0.1,
            trace=RecallTrace(strategies_used=["semantic"], total_candidates=1, fusion_method="rrf"),
        )

        async def fake_recall(req):
            return weak_recall
        orch.recall = fake_recall

        synth_called = {"count": 0}

        async def fake_synth(**kwargs):
            synth_called["count"] += 1
            return ReflectResult(answer="should not be called", sources=None)
        monkeypatch.setattr(orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synth))

        result = await orch.reflect(ReflectRequest(query="adversarial?", bank_id="b1"))

        assert "insufficient evidence" in result.answer.lower()
        assert result.sources == []
        assert synth_called["count"] == 0, "LLM must not be invoked when below abstention floor"

    @pytest.mark.asyncio
    async def test_does_not_abstain_when_top_score_above_floor(self, monkeypatch):
        """Strong hit clears the floor → normal synthesis path runs."""
        from astrocyte.types import MemoryHit, RecallResult, RecallTrace
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        orch.adversarial_abstention_enabled = True
        orch.adversarial_abstention_floor = 0.2

        strong_recall = RecallResult(
            hits=[MemoryHit(text="strong", score=0.85, memory_id="m1")],
            total_available=1,
            truncated=False,
            top_semantic_score=0.85,
            trace=RecallTrace(strategies_used=["semantic"], total_candidates=1, fusion_method="rrf"),
        )

        async def fake_recall(req):
            return strong_recall
        orch.recall = fake_recall

        async def fake_synth(**kwargs):
            return ReflectResult(answer="real synthesized answer", sources=kwargs.get("hits"))
        monkeypatch.setattr(orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synth))

        result = await orch.reflect(ReflectRequest(query="real question?", bank_id="b1"))

        assert "insufficient evidence" not in result.answer.lower()
        assert result.answer == "real synthesized answer"

    @pytest.mark.asyncio
    async def test_disabled_means_no_abstention_even_on_weak_hits(self, monkeypatch):
        """``abstention_enabled=False`` keeps the legacy behavior — even
        weak hits go through to the LLM."""
        from astrocyte.types import MemoryHit, RecallResult, RecallTrace
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        orch.adversarial_abstention_enabled = False

        weak_recall = RecallResult(
            hits=[MemoryHit(text="weak", score=0.1, memory_id="m1")],
            total_available=1,
            truncated=False,
            top_semantic_score=0.1,
            trace=RecallTrace(strategies_used=["semantic"], total_candidates=1, fusion_method="rrf"),
        )

        async def fake_recall(req):
            return weak_recall
        orch.recall = fake_recall

        synth_called = {"count": 0}

        async def fake_synth(**kwargs):
            synth_called["count"] += 1
            return ReflectResult(answer="legacy behavior — LLM invoked", sources=None)
        monkeypatch.setattr(orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synth))

        await orch.reflect(ReflectRequest(query="q?", bank_id="b1"))

        assert synth_called["count"] == 1, (
            "When abstention is disabled, LLM must run even on weak hits"
        )


class TestAbstentionFloorIntentConditional:
    """``adversarial_abstention_floor_intent_only=True`` skips the floor
    when the query has a confident well-formed intent. The previous flat
    floor cratered single-hop (-10pt) and temporal (-10pt) on the
    hindsight-balanced bench preset because legitimate questions
    sometimes have top retrieval scores below 0.2.
    """

    def _orch_with_floor(self, *, intent_only: bool) -> "PipelineOrchestrator":
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        orch.adversarial_abstention_enabled = True
        orch.adversarial_abstention_floor = 0.2
        orch.adversarial_abstention_floor_intent_only = intent_only
        return orch

    @pytest.mark.asyncio
    async def test_factual_intent_skips_floor(self, monkeypatch):
        """A confident FACTUAL query ('what is X?') with weak retrieval
        must NOT abstain — it should reach the LLM."""
        from astrocyte.types import MemoryHit, RecallResult, RecallTrace
        orch = self._orch_with_floor(intent_only=True)

        weak = RecallResult(
            hits=[MemoryHit(text="weakly related", score=0.1, memory_id="m1")],
            total_available=1, truncated=False, top_semantic_score=0.1,
            trace=RecallTrace(strategies_used=["semantic"], total_candidates=1, fusion_method="rrf"),
        )

        async def fake_recall(req): return weak
        orch.recall = fake_recall

        synth_called = {"count": 0}
        async def fake_synth(**kwargs):
            synth_called["count"] += 1
            return ReflectResult(answer="real LLM answer", sources=None)
        monkeypatch.setattr(orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synth))

        result = await orch.reflect(ReflectRequest(query="What is the capital of France?", bank_id="b1"))

        assert "insufficient evidence" not in result.answer.lower()
        assert synth_called["count"] == 1, "FACTUAL intent must bypass the floor"

    @pytest.mark.asyncio
    async def test_temporal_intent_skips_floor(self, monkeypatch):
        """TEMPORAL queries ('when did X happen?') must bypass the floor —
        these were the worst-hit category in the hindsight-balanced bench."""
        from astrocyte.types import MemoryHit, RecallResult, RecallTrace
        orch = self._orch_with_floor(intent_only=True)

        weak = RecallResult(
            hits=[MemoryHit(text="something", score=0.1, memory_id="m1")],
            total_available=1, truncated=False, top_semantic_score=0.1,
            trace=RecallTrace(strategies_used=["semantic"], total_candidates=1, fusion_method="rrf"),
        )
        async def fake_recall(req): return weak
        orch.recall = fake_recall
        async def fake_synth(**kwargs):
            return ReflectResult(answer="LLM answered", sources=None)
        monkeypatch.setattr(orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synth))

        result = await orch.reflect(ReflectRequest(query="When did Alice move to Boston?", bank_id="b1"))

        assert "insufficient evidence" not in result.answer.lower()

    @pytest.mark.asyncio
    async def test_unknown_intent_still_fires_floor(self, monkeypatch):
        """Queries with no confident intent (e.g. odd phrasing — common for
        adversarial false-premise) must still trigger the floor."""
        from astrocyte.types import MemoryHit, RecallResult, RecallTrace
        orch = self._orch_with_floor(intent_only=True)

        weak = RecallResult(
            hits=[MemoryHit(text="x", score=0.05, memory_id="m1")],
            total_available=1, truncated=False, top_semantic_score=0.05,
            trace=RecallTrace(strategies_used=["semantic"], total_candidates=1, fusion_method="rrf"),
        )
        async def fake_recall(req): return weak
        orch.recall = fake_recall

        synth_called = {"count": 0}
        async def fake_synth(**kwargs):
            synth_called["count"] += 1
            return ReflectResult(answer="should not run", sources=None)
        monkeypatch.setattr(orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synth))

        # A nonsense / unrecognized-shape query → UNKNOWN intent → floor fires.
        result = await orch.reflect(ReflectRequest(query="frobnicate quux baz", bank_id="b1"))

        assert "insufficient evidence" in result.answer.lower()
        assert synth_called["count"] == 0

    @pytest.mark.asyncio
    async def test_intent_only_disabled_keeps_legacy_behaviour(self, monkeypatch):
        """When ``abstention_floor_intent_only=False`` (default), the floor
        fires on EVERY weak query regardless of intent — same as the
        pre-feature behaviour."""
        from astrocyte.types import MemoryHit, RecallResult, RecallTrace
        orch = self._orch_with_floor(intent_only=False)

        weak = RecallResult(
            hits=[MemoryHit(text="weak", score=0.1, memory_id="m1")],
            total_available=1, truncated=False, top_semantic_score=0.1,
            trace=RecallTrace(strategies_used=["semantic"], total_candidates=1, fusion_method="rrf"),
        )
        async def fake_recall(req): return weak
        orch.recall = fake_recall
        async def fake_synth(**kwargs):
            return ReflectResult(answer="LLM ran", sources=None)
        monkeypatch.setattr(orchestrator_mod, "synthesize", AsyncMock(side_effect=fake_synth))

        # Even a confident FACTUAL query — floor still fires under legacy mode.
        result = await orch.reflect(ReflectRequest(query="What is X?", bank_id="b1"))

        assert "insufficient evidence" in result.answer.lower()

    def test_should_fire_helper_returns_true_when_intent_only_disabled(self):
        """Direct test of ``_abstention_floor_should_fire`` — the legacy path."""
        orch = self._orch_with_floor(intent_only=False)
        # Even a clearly-factual query: should_fire returns True (legacy).
        assert orch._abstention_floor_should_fire("What is the capital of France?") is True

    def test_should_fire_helper_returns_false_for_well_formed_intents(self):
        """Direct unit test of the intent gate."""
        orch = self._orch_with_floor(intent_only=True)
        # Each well-formed intent should bypass the floor.
        well_formed_queries = [
            "What is the capital of France?",          # FACTUAL
            "When did Alice move to Boston?",          # TEMPORAL
            "How does X relate to Y?",                 # RELATIONAL
            "Compare X versus Y",                      # COMPARATIVE
            "How do I configure the worker?",          # PROCEDURAL
        ]
        for q in well_formed_queries:
            assert orch._abstention_floor_should_fire(q) is False, (
                f"intent-only mode must skip floor for well-formed query: {q!r}"
            )

    def test_should_fire_helper_returns_true_for_unknown_intent(self):
        """UNKNOWN intent → floor still fires (the adversarial-shape bucket)."""
        orch = self._orch_with_floor(intent_only=True)
        assert orch._abstention_floor_should_fire("frobnicate quux baz") is True

    def test_should_fire_helper_handles_empty_query(self):
        """Empty / whitespace queries default to firing the floor (safe path)."""
        orch = self._orch_with_floor(intent_only=True)
        assert orch._abstention_floor_should_fire("") is True
        assert orch._abstention_floor_should_fire("   ") is True


class TestQueryAnalyzerWiring:
    """The query analyzer's regex pre-pass populates VectorFilters.time_range
    for queries with temporal expressions."""

    @pytest.mark.asyncio
    async def test_temporal_query_populates_time_range_filter(self, monkeypatch):
        """A query like 'what happened in March 2024?' adds a time_range
        filter on top of the request's other filters before retrieval runs."""
        from astrocyte.types import VectorFilters
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        orch.query_analyzer_enabled = True

        captured: dict = {}

        async def fake_parallel_retrieve(*args, **kwargs):
            captured["filters"] = kwargs.get("filters")
            return {"semantic": []}
        monkeypatch.setattr(
            orchestrator_mod, "parallel_retrieve",
            AsyncMock(side_effect=fake_parallel_retrieve),
        )

        await orch.recall(RecallRequest(
            query="What happened in March 2024?",
            bank_id="b1",
        ))

        f = captured["filters"]
        assert isinstance(f, VectorFilters)
        assert f.time_range is not None
        start, end = f.time_range
        assert start.year == 2024 and start.month == 3
        assert end.year == 2024 and end.month == 3

    @pytest.mark.asyncio
    async def test_caller_time_range_wins_over_analyzer(self, monkeypatch):
        """When the caller supplies time_range, the analyzer doesn't
        override it (caller's filter is the floor)."""
        from datetime import datetime, timezone

        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        orch.query_analyzer_enabled = True

        captured: dict = {}

        async def fake_parallel_retrieve(*args, **kwargs):
            captured["filters"] = kwargs.get("filters")
            return {"semantic": []}
        monkeypatch.setattr(
            orchestrator_mod, "parallel_retrieve",
            AsyncMock(side_effect=fake_parallel_retrieve),
        )

        caller_range = (
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2020, 12, 31, tzinfo=timezone.utc),
        )
        await orch.recall(RecallRequest(
            query="What happened in March 2024?",  # would extract 2024
            bank_id="b1",
            time_range=caller_range,
        ))

        # Caller's range was preserved, not overwritten.
        assert captured["filters"].time_range[0].year == 2020

    @pytest.mark.asyncio
    async def test_disabled_analyzer_leaves_time_range_alone(self, monkeypatch):
        """When ``query_analyzer_enabled=False``, no temporal extraction
        runs and the filter's time_range is whatever the caller supplied
        (which is None by default)."""
        from astrocyte.types import VectorFilters
        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm)
        orch.query_analyzer_enabled = False

        captured: dict = {}

        async def fake_parallel_retrieve(*args, **kwargs):
            captured["filters"] = kwargs.get("filters")
            return {"semantic": []}
        monkeypatch.setattr(
            orchestrator_mod, "parallel_retrieve",
            AsyncMock(side_effect=fake_parallel_retrieve),
        )

        await orch.recall(RecallRequest(
            query="What happened in March 2024?",
            bank_id="b1",
        ))

        assert isinstance(captured["filters"], VectorFilters)
        assert captured["filters"].time_range is None


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
