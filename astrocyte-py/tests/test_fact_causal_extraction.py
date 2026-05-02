"""Tests for fact-level cause→effect link extraction (Hindsight parity, C2).

Covers:

1. Index-based extraction (the LLM returns ``source_fact_index``,
   ``target_fact_index``).
2. Out-of-range indices dropped.
3. Self-loops dropped.
4. Below-confidence relations dropped.
5. ``max_pairs_per_fact`` enforced per source.
6. Duplicates deduped.
7. Malformed JSON returns ``[]``.
8. ``< 2`` chunks short-circuits without LLM call.
9. ``build_memory_links_from_relations`` resolves indices to memory IDs
   and produces ``MemoryLink`` objects with ``link_type="caused_by"``.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.fact_causal_extraction import (
    FactCausalRelation,
    build_memory_links_from_relations,
    extract_fact_causal_relations,
)
from astrocyte.testing.in_memory import MockLLMProvider
from astrocyte.types import Completion, MemoryLink, Message, TokenUsage


class _ScriptedLLM(MockLLMProvider):
    """MockLLMProvider returning a canned response and counting calls."""

    def __init__(self, response: str) -> None:
        super().__init__(default_response=response)
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
        self.call_count += 1
        return Completion(
            text=self._default_response,
            model=model or "mock",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )


CHUNKS_BURNOUT = [
    "Alice worked 80-hour weeks for six months straight.",
    "Alice felt completely burned out by mid-July.",
    "Alice resigned from her job that August.",
]


class TestExtractFactCausalRelations:
    @pytest.mark.asyncio
    async def test_index_pair_extraction(self):
        """Standard cause→effect at the fact (chunk) level."""
        llm = _ScriptedLLM(
            '[{"source_fact_index": 1, "target_fact_index": 0,'
            ' "relation_type": "caused_by",'
            ' "evidence": "80-hour weeks", "confidence": 0.9},'
            ' {"source_fact_index": 2, "target_fact_index": 1,'
            ' "relation_type": "caused_by",'
            ' "evidence": "burned out", "confidence": 0.85}]'
        )

        relations = await extract_fact_causal_relations(CHUNKS_BURNOUT, llm)

        assert len(relations) == 2
        assert (relations[0].source_fact_index, relations[0].target_fact_index) == (1, 0)
        assert (relations[1].source_fact_index, relations[1].target_fact_index) == (2, 1)
        assert relations[0].evidence == "80-hour weeks"

    @pytest.mark.asyncio
    async def test_out_of_range_indices_dropped(self):
        """LLM hallucinated index beyond the batch — drop silently."""
        llm = _ScriptedLLM(
            '[{"source_fact_index": 99, "target_fact_index": 0,'
            ' "evidence": "...", "confidence": 0.9},'
            ' {"source_fact_index": 1, "target_fact_index": 0,'
            ' "evidence": "...", "confidence": 0.9}]'
        )

        relations = await extract_fact_causal_relations(CHUNKS_BURNOUT, llm)

        assert len(relations) == 1
        assert relations[0].source_fact_index == 1

    @pytest.mark.asyncio
    async def test_self_loops_dropped(self):
        llm = _ScriptedLLM(
            '[{"source_fact_index": 1, "target_fact_index": 1,'
            ' "evidence": "x", "confidence": 0.9}]'
        )

        relations = await extract_fact_causal_relations(CHUNKS_BURNOUT, llm)

        assert relations == []

    @pytest.mark.asyncio
    async def test_low_confidence_dropped(self):
        llm = _ScriptedLLM(
            '[{"source_fact_index": 1, "target_fact_index": 0,'
            ' "evidence": "weak", "confidence": 0.3}]'
        )

        relations = await extract_fact_causal_relations(
            CHUNKS_BURNOUT, llm, min_confidence=0.6,
        )

        assert relations == []

    @pytest.mark.asyncio
    async def test_max_pairs_per_fact_enforced(self):
        """Source can have at most ``max_pairs_per_fact`` causes."""
        llm = _ScriptedLLM(
            '[{"source_fact_index": 2, "target_fact_index": 0, "evidence": "a", "confidence": 0.9},'
            ' {"source_fact_index": 2, "target_fact_index": 1, "evidence": "b", "confidence": 0.85},'
            ' {"source_fact_index": 2, "target_fact_index": 0, "evidence": "c", "confidence": 0.8}]'
        )

        relations = await extract_fact_causal_relations(
            CHUNKS_BURNOUT, llm, max_pairs_per_fact=2,
        )

        assert len(relations) == 2  # Third pair dropped (cap + dedup)
        assert all(r.source_fact_index == 2 for r in relations)

    @pytest.mark.asyncio
    async def test_duplicate_pairs_deduped(self):
        llm = _ScriptedLLM(
            '[{"source_fact_index": 1, "target_fact_index": 0, "evidence": "a", "confidence": 0.9},'
            ' {"source_fact_index": 1, "target_fact_index": 0, "evidence": "b", "confidence": 0.85}]'
        )

        relations = await extract_fact_causal_relations(CHUNKS_BURNOUT, llm)

        assert len(relations) == 1

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self):
        llm = _ScriptedLLM("not json at all")

        relations = await extract_fact_causal_relations(CHUNKS_BURNOUT, llm)

        assert relations == []

    @pytest.mark.asyncio
    async def test_fewer_than_two_chunks_short_circuits(self):
        llm = _ScriptedLLM("[]")

        relations = await extract_fact_causal_relations(["only one"], llm)

        assert relations == []
        assert llm.call_count == 0


class TestBuildMemoryLinksFromRelations:
    def test_resolves_indices_to_memory_ids(self):
        relations = [
            FactCausalRelation(
                source_fact_index=1, target_fact_index=0,
                evidence="80-hour weeks", confidence=0.9,
            ),
            FactCausalRelation(
                source_fact_index=2, target_fact_index=1,
                evidence="burned out", confidence=0.85,
            ),
        ]
        memory_ids = ["mem-A", "mem-B", "mem-C"]

        links = build_memory_links_from_relations(
            relations, memory_ids, bank_id="b1",
        )

        assert len(links) == 2
        assert isinstance(links[0], MemoryLink)
        assert links[0].source_memory_id == "mem-B"  # index 1
        assert links[0].target_memory_id == "mem-A"  # index 0
        assert links[0].link_type == "caused_by"
        assert links[0].evidence == "80-hour weeks"
        assert links[0].confidence == 0.9
        assert links[1].source_memory_id == "mem-C"
        assert links[1].target_memory_id == "mem-B"

    def test_drops_relations_with_indices_beyond_memory_ids(self):
        """Defensive: if chunking produced fewer memories than the
        LLM saw chunks, drop the now-invalid relations."""
        relations = [
            FactCausalRelation(
                source_fact_index=99, target_fact_index=0,
                evidence="x", confidence=0.9,
            ),
            FactCausalRelation(
                source_fact_index=1, target_fact_index=0,
                evidence="y", confidence=0.9,
            ),
        ]
        memory_ids = ["mem-A", "mem-B"]  # only 2 memories

        links = build_memory_links_from_relations(
            relations, memory_ids, bank_id="b1",
        )

        assert len(links) == 1
        assert links[0].source_memory_id == "mem-B"

    def test_empty_relations_returns_empty(self):
        assert build_memory_links_from_relations([], ["a", "b"], bank_id="b1") == []
