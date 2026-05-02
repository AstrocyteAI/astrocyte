"""Tests for structured 5-dimension fact extraction.

Two layers:

1. ``extract_facts`` — LLM-driven structured extraction. Tests use a
   scripted MockLLM with canned JSON responses to verify parsing,
   field defaults, type validation, entity dedup, and causal-relation
   parsing.

2. ``materialize_facts`` — pure-Python conversion of ExtractedFact
   list into VectorItems + Entities + MemoryLinks. Tests verify
   deterministic entity IDs, association mapping, and causal-edge
   resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astrocyte.pipeline.fact_extraction import (
    ExtractedFact,
    FactCausalRelation,
    FactEntity,
    extract_facts,
    extract_facts_verbatim,
    materialize_facts,
)
from astrocyte.testing.in_memory import MockLLMProvider
from astrocyte.types import Completion, MemoryLink, Message, TokenUsage


class _ScriptedLLM(MockLLMProvider):
    """MockLLM returning a single canned JSON response."""

    def __init__(self, response: str) -> None:
        super().__init__(default_response=response)
        self.call_count = 0
        self.last_user_prompt: str | None = None

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
        for m in messages:
            if m.role == "user" and isinstance(m.content, str):
                self.last_user_prompt = m.content
        return Completion(
            text=self._default_response,
            model="mock",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )


# ---------------------------------------------------------------------------
# extract_facts — parsing + field handling
# ---------------------------------------------------------------------------


class TestExtractFacts:
    @pytest.mark.asyncio
    async def test_parses_full_5_dimension_fact(self):
        """A complete fact with all 5 dimensions + entities + causal."""
        llm = _ScriptedLLM(
            '{"facts": [{'
            '"what": "Alice joined Google",'
            '"when": "last spring",'
            '"where": "Mountain View",'
            '"who": "Alice (subject)",'
            '"why": "research opportunities",'
            '"fact_type": "world",'
            '"occurred_start": "2024-04-01T00:00:00Z",'
            '"occurred_end": null,'
            '"entities": [{"name": "Alice", "entity_type": "PERSON"},'
            '             {"name": "Google", "entity_type": "ORG"}],'
            '"causal_relations": [{"target_fact_index": 1, "strength": 0.9}]'
            '}]}'
        )

        facts = await extract_facts("source text", llm)

        assert len(facts) == 1
        f = facts[0]
        assert f.what == "Alice joined Google"
        assert f.when == "last spring"
        assert f.where == "Mountain View"
        assert f.who == "Alice (subject)"
        assert f.why == "research opportunities"
        assert f.fact_type == "world"
        assert f.occurred_start == datetime(2024, 4, 1, tzinfo=UTC)
        assert f.occurred_end is None
        assert len(f.entities) == 2
        assert f.entities[0].name == "Alice"
        assert f.entities[0].entity_type == "PERSON"
        assert len(f.causal_relations) == 1
        assert f.causal_relations[0].target_fact_index == 1
        assert f.causal_relations[0].strength == 0.9

    @pytest.mark.asyncio
    async def test_missing_what_drops_fact(self):
        """``what`` is required — facts without it are silently dropped."""
        llm = _ScriptedLLM(
            '{"facts": ['
            '{"when": "today"},'  # no what → drop
            '{"what": "Bob ran a marathon"}'
            ']}'
        )

        facts = await extract_facts("source", llm)

        assert len(facts) == 1
        assert facts[0].what == "Bob ran a marathon"

    @pytest.mark.asyncio
    async def test_defaults_for_missing_dimensions(self):
        """Missing dimensions default to "N/A"; missing fact_type
        defaults to "experience"."""
        llm = _ScriptedLLM('{"facts": [{"what": "Alice likes Python"}]}')

        facts = await extract_facts("text", llm)

        assert facts[0].when == "N/A"
        assert facts[0].where == "N/A"
        assert facts[0].who == "N/A"
        assert facts[0].why == "N/A"
        assert facts[0].fact_type == "experience"
        assert facts[0].entities == []
        assert facts[0].causal_relations == []

    @pytest.mark.asyncio
    async def test_invalid_fact_type_normalizes_to_experience(self):
        """Unknown fact_type strings fall back to "experience"."""
        llm = _ScriptedLLM(
            '{"facts": [{"what": "X", "fact_type": "garbage"}]}'
        )

        facts = await extract_facts("text", llm)

        assert facts[0].fact_type == "experience"

    @pytest.mark.asyncio
    async def test_max_facts_caps_output(self):
        """``max_facts`` enforces a hard cap on returned facts."""
        big = '{"facts": [' + ",".join(
            f'{{"what": "fact {i}"}}' for i in range(50)
        ) + "]}"
        llm = _ScriptedLLM(big)

        facts = await extract_facts("text", llm, max_facts=5)

        assert len(facts) == 5

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self):
        llm = _ScriptedLLM("not JSON")
        assert await extract_facts("text", llm) == []

    @pytest.mark.asyncio
    async def test_empty_text_skips_llm(self):
        llm = _ScriptedLLM("{}")
        assert await extract_facts("", llm) == []
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_event_date_passed_into_user_prompt(self):
        """When ``event_date`` is supplied, the prompt includes it as a
        reference for resolving relative time expressions."""
        llm = _ScriptedLLM('{"facts": []}')
        ref_date = datetime(2024, 6, 15, tzinfo=UTC)

        await extract_facts("text", llm, event_date=ref_date)

        assert llm.last_user_prompt is not None
        assert "2024-06-15" in llm.last_user_prompt

    @pytest.mark.asyncio
    async def test_iso_with_z_suffix_parses(self):
        """ISO timestamps ending in 'Z' parse correctly (Python 3.11+
        handles it, but we're explicit)."""
        llm = _ScriptedLLM(
            '{"facts": [{"what": "X", "occurred_start": "2024-04-01T00:00:00Z"}]}'
        )
        facts = await extract_facts("text", llm)
        assert facts[0].occurred_start == datetime(2024, 4, 1, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_invalid_iso_becomes_none(self):
        """Garbage timestamps become None instead of raising."""
        llm = _ScriptedLLM(
            '{"facts": [{"what": "X", "occurred_start": "not a date"}]}'
        )
        facts = await extract_facts("text", llm)
        assert facts[0].occurred_start is None

    @pytest.mark.asyncio
    async def test_handles_code_fences(self):
        """LLM response wrapped in ```json ... ``` is unwrapped."""
        llm = _ScriptedLLM(
            '```json\n{"facts": [{"what": "X"}]}\n```'
        )
        facts = await extract_facts("text", llm)
        assert len(facts) == 1


# ---------------------------------------------------------------------------
# materialize_facts — translation to retain artefacts
# ---------------------------------------------------------------------------


class TestExtractFactsVerbatim:
    """Verbatim mode preserves the original chunk text and only adds
    structured metadata. Critical for benchmarks where question
    embeddings need to match against the original conversation
    vocabulary (the LoCoMo recall_hit_rate regression that motivated
    the redesign)."""

    @pytest.mark.asyncio
    async def test_what_is_chunk_text_verbatim(self):
        """``ExtractedFact.what`` MUST equal the input chunk text exactly,
        regardless of what the LLM returns in its 'what' field."""
        # LLM helpfully tries to paraphrase; verbatim mode ignores that
        # and uses the chunk text instead.
        llm = _ScriptedLLM(
            '{"facts": ['
            '{"what": "LLM PARAPHRASE 0", "who": "Alice", '
            ' "entities": [{"name": "Alice", "entity_type": "PERSON"}]},'
            '{"what": "LLM PARAPHRASE 1", "who": "Bob",'
            ' "entities": [{"name": "Bob", "entity_type": "PERSON"}]}'
            ']}'
        )

        chunks = [
            "Alice went hiking yesterday and had a great time.",
            "Bob played chess in the park with his friend.",
        ]

        facts = await extract_facts_verbatim(chunks, llm)

        assert len(facts) == 2
        assert facts[0].what == chunks[0], (
            "Verbatim mode must use chunk text as 'what', not the LLM paraphrase"
        )
        assert facts[1].what == chunks[1]
        # Metadata still comes from the LLM.
        assert facts[0].who == "Alice"
        assert facts[1].who == "Bob"
        assert facts[0].entities[0].name == "Alice"
        assert facts[1].entities[0].name == "Bob"

    @pytest.mark.asyncio
    async def test_returns_one_fact_per_chunk_in_order(self):
        """Output length and order MUST match the input chunks list."""
        llm = _ScriptedLLM('{"facts": ['
            '{"who": "A"}, {"who": "B"}, {"who": "C"}'
        ']}')
        chunks = ["chunk-A", "chunk-B", "chunk-C"]

        facts = await extract_facts_verbatim(chunks, llm)

        assert [f.what for f in facts] == chunks
        assert [f.who for f in facts] == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_handles_llm_returning_fewer_facts_than_chunks(self):
        """When the LLM emits fewer entries than chunks, the trailing
        chunks get empty-metadata facts (chunk text preserved)."""
        llm = _ScriptedLLM('{"facts": [{"who": "Alice"}]}')  # only 1 entry
        chunks = ["A", "B", "C"]

        facts = await extract_facts_verbatim(chunks, llm)

        assert len(facts) == 3, "One fact per chunk regardless of LLM output length"
        assert facts[0].who == "Alice"
        assert facts[1].who == "N/A"
        assert facts[2].who == "N/A"
        assert [f.what for f in facts] == chunks

    @pytest.mark.asyncio
    async def test_causal_relations_indices_into_chunks(self):
        """``target_fact_index`` references chunk position (= memory
        position after materialization)."""
        llm = _ScriptedLLM(
            '{"facts": ['
            '{"who": "cause"},'
            '{"who": "effect", "causal_relations": [{"target_fact_index": 0, "strength": 0.9}]}'
            ']}'
        )
        chunks = ["she worked overtime", "she felt burned out"]

        facts = await extract_facts_verbatim(chunks, llm)

        assert facts[1].causal_relations[0].target_fact_index == 0
        assert facts[1].causal_relations[0].strength == 0.9

    @pytest.mark.asyncio
    async def test_self_loop_causal_dropped(self):
        llm = _ScriptedLLM(
            '{"facts": [{"causal_relations": [{"target_fact_index": 0, "strength": 0.9}]}]}'
        )
        facts = await extract_facts_verbatim(["only-chunk"], llm)
        assert facts[0].causal_relations == []

    @pytest.mark.asyncio
    async def test_out_of_range_causal_dropped(self):
        llm = _ScriptedLLM(
            '{"facts": ['
            '{"causal_relations": [{"target_fact_index": 99, "strength": 0.9}]}'
            ']}'
        )
        facts = await extract_facts_verbatim(["chunk-0"], llm)
        assert facts[0].causal_relations == []

    @pytest.mark.asyncio
    async def test_empty_chunks_returns_empty_no_llm_call(self):
        llm = _ScriptedLLM("{}")
        facts = await extract_facts_verbatim([], llm)
        assert facts == []
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_all_blank_chunks_returns_empty(self):
        llm = _ScriptedLLM("{}")
        facts = await extract_facts_verbatim(["", "  ", "\n"], llm)
        assert facts == []
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self):
        llm = _ScriptedLLM("not json")
        facts = await extract_facts_verbatim(["chunk-A"], llm)
        assert facts == []


class TestMaterializeFactsVerbatimFlag:
    """``materialize_facts(verbatim=True)`` uses ``fact.what`` directly
    (no Involving/At/When decorations)."""

    def test_verbatim_text_is_chunk_text_not_decorated(self):
        facts = [
            ExtractedFact(
                what="Alice went hiking yesterday.",
                who="Alice", when="yesterday", where="N/A",
            ),
        ]
        materialized = materialize_facts(facts, bank_id="b1", verbatim=True)
        # Stored text is the raw chunk, NOT "Alice went hiking yesterday. | Involving: Alice | When: yesterday"
        assert materialized.vector_items[0].text == "Alice went hiking yesterday."

    def test_concise_default_decorates_text(self):
        """Concise mode (default, verbatim=False) keeps the build_text
        decoration behavior."""
        facts = [
            ExtractedFact(
                what="Alice went hiking",
                who="Alice", when="yesterday",
            ),
        ]
        materialized = materialize_facts(facts, bank_id="b1")  # verbatim=False default
        text = materialized.vector_items[0].text
        assert "Alice went hiking" in text
        assert "Involving:" in text or "When:" in text, (
            "Concise mode must include build_text decorations"
        )

    def test_verbatim_metadata_still_populated(self):
        """Even in verbatim mode, structured fields go into _fact_* metadata."""
        facts = [
            ExtractedFact(
                what="Alice went hiking yesterday.",
                who="Alice", when="yesterday",
            ),
        ]
        materialized = materialize_facts(facts, bank_id="b1", verbatim=True)
        meta = materialized.vector_items[0].metadata
        assert meta["_fact_who"] == "Alice"
        assert meta["_fact_when"] == "yesterday"


class TestMaterializeFacts:
    def test_one_vector_item_per_fact(self):
        facts = [
            ExtractedFact(what="A", entities=[FactEntity(name="X", entity_type="PERSON")]),
            ExtractedFact(what="B", entities=[FactEntity(name="Y", entity_type="PERSON")]),
        ]

        result = materialize_facts(facts, bank_id="b1")

        assert len(result.vector_items) == 2
        assert result.vector_items[0].text == "A"
        assert result.vector_items[1].text == "B"
        # Bank ID propagates.
        assert all(v.bank_id == "b1" for v in result.vector_items)

    def test_entities_deduplicated_by_name_and_type(self):
        """Same (name, type) across multiple facts → one Entity in
        the output, with a deterministic ID."""
        facts = [
            ExtractedFact(what="A", entities=[FactEntity(name="Alice", entity_type="PERSON")]),
            ExtractedFact(what="B", entities=[FactEntity(name="Alice", entity_type="PERSON")]),
            ExtractedFact(what="C", entities=[FactEntity(name="Bob", entity_type="PERSON")]),
        ]

        result = materialize_facts(facts, bank_id="b1")

        assert len(result.entities) == 2
        names = {e.name for e in result.entities}
        assert names == {"Alice", "Bob"}
        # Deterministic IDs based on (type, slug(name)).
        alice = next(e for e in result.entities if e.name == "Alice")
        assert alice.id == "person:alice"

    def test_associations_link_each_memory_to_its_entities(self):
        facts = [
            ExtractedFact(what="A meets B", entities=[
                FactEntity(name="Alice", entity_type="PERSON"),
                FactEntity(name="Bob", entity_type="PERSON"),
            ]),
        ]

        result = materialize_facts(facts, bank_id="b1")

        item_id = result.vector_items[0].id
        # One association per entity in the fact.
        assert len(result.memory_entity_associations) == 2
        for mem_id, ent_id in result.memory_entity_associations:
            assert mem_id == item_id

    def test_causal_relations_become_memory_links(self):
        """A fact with ``caused_by`` references produces directional
        MemoryLinks resolved from indices to memory IDs."""
        facts = [
            ExtractedFact(what="cause"),
            ExtractedFact(what="effect", causal_relations=[
                FactCausalRelation(target_fact_index=0, strength=0.9),
            ]),
        ]

        result = materialize_facts(facts, bank_id="b1")

        cause_id = result.vector_items[0].id
        effect_id = result.vector_items[1].id
        assert len(result.memory_links) == 1
        link = result.memory_links[0]
        assert isinstance(link, MemoryLink)
        assert link.source_memory_id == effect_id  # source = effect
        assert link.target_memory_id == cause_id   # target = cause
        assert link.link_type == "caused_by"
        assert link.confidence == pytest.approx(0.9)

    def test_self_loops_dropped(self):
        facts = [
            ExtractedFact(what="X", causal_relations=[
                FactCausalRelation(target_fact_index=0),
            ]),
        ]
        assert materialize_facts(facts, bank_id="b1").memory_links == []

    def test_out_of_range_indices_dropped(self):
        facts = [
            ExtractedFact(what="A"),
            ExtractedFact(what="B", causal_relations=[
                FactCausalRelation(target_fact_index=99),
            ]),
        ]
        assert materialize_facts(facts, bank_id="b1").memory_links == []

    def test_structured_dimensions_promoted_to_metadata(self):
        """when/where/who/why and occurred_start show up under
        ``_fact_*`` metadata keys."""
        facts = [
            ExtractedFact(
                what="Alice joined Google",
                when="last spring",
                where="Mountain View",
                who="Alice",
                why="for research",
                fact_type="world",
                occurred_start=datetime(2024, 4, 1, tzinfo=UTC),
            ),
        ]

        result = materialize_facts(facts, bank_id="b1")

        meta = result.vector_items[0].metadata
        assert meta["_fact_when"] == "last spring"
        assert meta["_fact_where"] == "Mountain View"
        assert meta["_fact_who"] == "Alice"
        assert meta["_fact_why"] == "for research"
        assert meta["_fact_type"] == "world"
        assert meta["_fact_occurred_start"] == "2024-04-01T00:00:00+00:00"

    def test_na_dimensions_omitted_from_metadata(self):
        """Don't pollute metadata with N/A defaults."""
        facts = [ExtractedFact(what="X")]  # all dims default N/A

        result = materialize_facts(facts, bank_id="b1")

        meta = result.vector_items[0].metadata
        for key in ("_fact_when", "_fact_where", "_fact_who", "_fact_why"):
            assert key not in meta

    def test_text_includes_dimensions_when_present(self):
        """build_text appends Involving/At/When sections for non-N/A
        dimensions, joined with ' | '."""
        facts = [
            ExtractedFact(
                what="Alice joined Google",
                who="Alice",
                why="for research",
                where="Mountain View",
            ),
        ]

        result = materialize_facts(facts, bank_id="b1")

        text = result.vector_items[0].text
        assert "Alice joined Google" in text
        assert "Involving:" in text and "Alice" in text
        assert "At:" in text and "Mountain View" in text
        assert "for research" in text

    def test_embeddings_threaded_through_when_provided(self):
        """When embeddings are supplied, they go onto the VectorItems."""
        facts = [ExtractedFact(what="A"), ExtractedFact(what="B")]
        embeddings = [[1.0, 0.0], [0.0, 1.0]]

        result = materialize_facts(
            facts, bank_id="b1", embeddings=embeddings,
        )

        assert result.vector_items[0].vector == [1.0, 0.0]
        assert result.vector_items[1].vector == [0.0, 1.0]

    def test_occurred_start_falls_through_to_occurred_at(self):
        """fact.occurred_start populates VectorItem.occurred_at;
        falls back to caller-supplied default when None."""
        default_time = datetime(2024, 1, 1, tzinfo=UTC)
        fact_time = datetime(2024, 6, 1, tzinfo=UTC)

        facts = [
            ExtractedFact(what="A", occurred_start=fact_time),
            ExtractedFact(what="B"),  # no time → uses default
        ]

        result = materialize_facts(
            facts, bank_id="b1", occurred_at=default_time,
        )

        assert result.vector_items[0].occurred_at == fact_time
        assert result.vector_items[1].occurred_at == default_time
