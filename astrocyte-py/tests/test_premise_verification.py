"""Tests for premise extraction + verification (adversarial defense).

Covers:

1. Empty / no-presupposition questions return [] from extraction →
   verify_question reports all_premises_supported=True.
2. Premise extraction parses JSON arrays correctly.
3. Verification: supported claim + high confidence → supported=True.
4. Verification: low confidence → supported=False even if LLM said true.
5. Verification: no recall hits → supported=False.
6. End-to-end: ANY unsupported premise sets all_premises_supported=False
   and short_circuit_message() returns a useful string.
7. End-to-end: all premises supported → no short-circuit.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.premise_verification import (
    Premise,
    extract_premises,
    verify_premise,
    verify_question,
)
from astrocyte.testing.in_memory import MockLLMProvider
from astrocyte.types import Completion, MemoryHit, Message, TokenUsage


class _ScriptedLLM(MockLLMProvider):
    """Returns a sequence of canned responses, one per call."""

    def __init__(self, responses: list[str]) -> None:
        super().__init__(default_response="")
        self._responses = list(responses)
        self.call_count = 0

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools=None,
        tool_choice=None,
    ) -> Completion:
        idx = min(self.call_count, len(self._responses) - 1)
        text = self._responses[idx]
        self.call_count += 1
        return Completion(
            text=text,
            model="mock",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )


class TestExtractPremises:
    @pytest.mark.asyncio
    async def test_extracts_atomic_claims_from_why_question(self):
        """'Why did Alice quit Google?' → presupposes Alice worked at
        Google AND quit. Two presuppositions extracted."""
        llm = _ScriptedLLM([
            '[{"claim": "Alice worked at Google", "rationale": "implied by `quit Google`"},'
            ' {"claim": "Alice quit Google", "rationale": "the question takes this as fact"}]'
        ])

        premises = await extract_premises(
            "Why did Alice quit Google?", llm,
        )

        assert len(premises) == 2
        assert premises[0].claim == "Alice worked at Google"
        assert premises[1].claim == "Alice quit Google"
        assert premises[0].rationale  # non-empty

    @pytest.mark.asyncio
    async def test_pure_lookup_returns_empty(self):
        """'What is the capital of France?' has no embedded
        presupposition — return []."""
        llm = _ScriptedLLM(["[]"])

        premises = await extract_premises(
            "What is the capital of France?", llm,
        )

        assert premises == []

    @pytest.mark.asyncio
    async def test_caps_at_three_presuppositions(self):
        """Even if LLM emits more, we keep at most 3."""
        llm = _ScriptedLLM([
            '[{"claim": "a", "rationale": "x"},'
            ' {"claim": "b", "rationale": "x"},'
            ' {"claim": "c", "rationale": "x"},'
            ' {"claim": "d", "rationale": "x"}]'
        ])

        premises = await extract_premises("q", llm)

        assert len(premises) == 3
        assert [p.claim for p in premises] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self):
        llm = _ScriptedLLM(["this is not JSON"])
        premises = await extract_premises("q", llm)
        assert premises == []

    @pytest.mark.asyncio
    async def test_empty_question_returns_empty(self):
        llm = _ScriptedLLM(["[]"])
        premises = await extract_premises("", llm)
        assert premises == []
        assert llm.call_count == 0


class TestVerifyPremise:
    @pytest.mark.asyncio
    async def test_supported_high_confidence_returns_supported_true(self):
        """LLM judge returns supported + confidence >= threshold → True."""
        llm = _ScriptedLLM([
            '{"supported": true, "confidence": 0.9,'
            ' "evidence_ids": ["m1"], "rationale": "directly attested"}'
        ])

        async def recall_fn(query: str, max_results: int):
            return [MemoryHit(text="Alice works at Google", score=0.9, memory_id="m1")]

        verdict = await verify_premise(
            Premise(claim="Alice works at Google"),
            recall_fn,
            llm,
            min_confidence=0.6,
        )

        assert verdict.supported is True
        assert verdict.confidence == 0.9
        assert verdict.evidence_ids == ["m1"]

    @pytest.mark.asyncio
    async def test_low_confidence_overrides_supported_true(self):
        """LLM says supported=True with confidence below threshold →
        we still mark it unsupported. Defensive against LLM optimism."""
        llm = _ScriptedLLM([
            '{"supported": true, "confidence": 0.4,'
            ' "evidence_ids": [], "rationale": "weak"}'
        ])

        async def recall_fn(query: str, max_results: int):
            return [MemoryHit(text="vague", score=0.3, memory_id="m1")]

        verdict = await verify_premise(
            Premise(claim="Alice worked at Google"),
            recall_fn,
            llm,
            min_confidence=0.6,
        )

        assert verdict.supported is False, (
            "Confidence below threshold must override supported=True"
        )

    @pytest.mark.asyncio
    async def test_no_recall_hits_yields_unsupported(self):
        """When recall returns empty, the LLM should say not-supported;
        we still verify the path holds together."""
        llm = _ScriptedLLM([
            '{"supported": false, "confidence": 0.95,'
            ' "evidence_ids": [], "rationale": "no evidence in memory"}'
        ])

        async def recall_fn(query: str, max_results: int):
            return []

        verdict = await verify_premise(
            Premise(claim="Alice worked at Google"),
            recall_fn,
            llm,
        )

        assert verdict.supported is False

    @pytest.mark.asyncio
    async def test_recall_failure_returns_unsupported(self):
        """When recall raises, we degrade to unsupported (defensive)."""
        llm = _ScriptedLLM(["{}"])

        async def failing_recall(query: str, max_results: int):
            raise RuntimeError("DB down")

        verdict = await verify_premise(
            Premise(claim="x"), failing_recall, llm,
        )

        assert verdict.supported is False
        assert "recall failed" in verdict.rationale.lower()


class TestVerifyQuestion:
    @pytest.mark.asyncio
    async def test_no_presuppositions_means_all_supported(self):
        """Pure-lookup question → all_premises_supported=True (vacuously),
        short_circuit_message=None."""
        llm = _ScriptedLLM(["[]"])

        async def recall_fn(query: str, max_results: int):
            return []

        result = await verify_question(
            "What is the capital of France?",
            recall_fn=recall_fn,
            llm_provider=llm,
        )

        assert result.all_premises_supported is True
        assert result.short_circuit_message() is None
        assert result.premises == []

    @pytest.mark.asyncio
    async def test_unsupported_premise_short_circuits_with_message(self):
        """One unsupported premise → all_premises_supported=False,
        short_circuit_message quotes the failing claim."""
        llm = _ScriptedLLM([
            # extraction
            '[{"claim": "Alice worked at Google", "rationale": "implied"}]',
            # verification: not supported
            '{"supported": false, "confidence": 0.9,'
            ' "evidence_ids": [], "rationale": "no memory mentions Google"}',
        ])

        async def recall_fn(query: str, max_results: int):
            return []

        result = await verify_question(
            "Why did Alice quit Google?",
            recall_fn=recall_fn,
            llm_provider=llm,
        )

        assert result.all_premises_supported is False
        msg = result.short_circuit_message()
        assert msg is not None
        assert "alice worked at google" in msg.lower()
        assert "insufficient evidence" in msg.lower()

    @pytest.mark.asyncio
    async def test_all_supported_does_not_short_circuit(self):
        llm = _ScriptedLLM([
            '[{"claim": "Alice works at Google", "rationale": "x"}]',
            '{"supported": true, "confidence": 0.9,'
            ' "evidence_ids": ["m1"], "rationale": "directly attested"}',
        ])

        async def recall_fn(query: str, max_results: int):
            return [MemoryHit(text="Alice works at Google", score=0.9, memory_id="m1")]

        result = await verify_question(
            "What does Alice do at Google?",
            recall_fn=recall_fn,
            llm_provider=llm,
        )

        assert result.all_premises_supported is True
        assert result.short_circuit_message() is None
