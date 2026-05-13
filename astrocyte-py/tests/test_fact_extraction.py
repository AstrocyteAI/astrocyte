"""Tests for structured 5-dimension fact extraction.

Two layers:

1. ``extract_facts_verbatim`` — LLM-driven per-chunk metadata
   extraction. Tests use a scripted MockLLM with canned JSON
   responses to verify parsing, field defaults, type validation,
   entity dedup, and causal-relation parsing. (The concise
   ``extract_facts`` path was removed in M9 — see commit message
   and ADR notes.)

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
        response_format: dict | None = None,
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
# extract_facts (legacy concise path) — REMOVED in M9.
#
# The TestExtractFacts class lived here. The legacy path caused severe
# recall_hit_rate degradation (2026-05-02 finding) because LLM-paraphrased
# chunk text lost the surface vocabulary that question embeddings rely on.
# Verbatim is now the only supported extraction mode; config validation
# rejects extraction_mode: concise with a pointer to this rationale.
#
# Coverage of the JSON parsing / field handling that TestExtractFacts
# exercised is now provided by TestExtractFactsVerbatim below — both
# paths share _parse_json_object / _parse_iso_datetime.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# extract_facts_verbatim — chunk text + LLM-derived metadata
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


class TestExtractFactsVerbatimParallel:
    """Per-chunk parallel verbatim extraction (Phase 3 of cost-control
    port).

    Verifies that one LLM call fires per chunk, results are reassembled
    in input order, and per-chunk failures don't lose neighboring
    chunks' metadata.
    """

    @pytest.mark.asyncio
    async def test_one_call_per_chunk(self):
        from astrocyte.pipeline.fact_extraction import (
            extract_facts_verbatim_parallel,
        )

        # Single-chunk schema: returns one metadata dict per call.
        llm = _ScriptedLLM(
            '{"when":"yesterday","where":"home","who":"Alice","why":"N/A",'
            '"fact_type":"experience","occurred_start":null,'
            '"occurred_end":null,"entities":[]}'
        )
        chunks = ["chunk one", "chunk two", "chunk three"]
        facts = await extract_facts_verbatim_parallel(chunks, llm)
        assert len(facts) == len(chunks)
        # One LLM call per chunk (vs the batched path's one call total).
        assert llm.call_count == len(chunks)
        # Order preserved.
        for i, fact in enumerate(facts):
            assert fact.what == chunks[i]
            assert fact.who == "Alice"
            # Per-chunk parallel always drops cross-chunk causal refs.
            assert fact.causal_relations == []

    @pytest.mark.asyncio
    async def test_max_concurrency_bounds_in_flight(self):
        """Semaphore caps concurrent LLM calls per session."""
        import asyncio

        from astrocyte.pipeline.fact_extraction import (
            extract_facts_verbatim_parallel,
        )

        in_flight = 0
        peak_in_flight = 0
        peak_lock = asyncio.Lock()

        class _CountingLLM(_ScriptedLLM):
            async def complete(self, messages, **kwargs):
                nonlocal in_flight, peak_in_flight
                async with peak_lock:
                    in_flight += 1
                    peak_in_flight = max(peak_in_flight, in_flight)
                try:
                    await asyncio.sleep(0.01)
                    return await super().complete(messages, **kwargs)
                finally:
                    async with peak_lock:
                        in_flight -= 1

        llm = _CountingLLM(
            '{"when":"N/A","where":"N/A","who":"N/A","why":"N/A",'
            '"fact_type":"world","occurred_start":null,'
            '"occurred_end":null,"entities":[]}'
        )
        chunks = [f"chunk-{i}" for i in range(10)]
        await extract_facts_verbatim_parallel(chunks, llm, max_concurrency=3)
        assert peak_in_flight <= 3, f"semaphore breached: peak={peak_in_flight}"

    @pytest.mark.asyncio
    async def test_per_chunk_failure_isolated(self):
        """One chunk's malformed JSON doesn't lose neighbors' metadata."""
        from astrocyte.pipeline.fact_extraction import (
            extract_facts_verbatim_parallel,
        )

        good = (
            '{"when":"today","where":"office","who":"Bob","why":"N/A",'
            '"fact_type":"experience","occurred_start":null,'
            '"occurred_end":null,"entities":[]}'
        )

        class _AlternatingLLM(MockLLMProvider):
            def __init__(self):
                super().__init__()
                self.call_count = 0

            async def complete(self, messages, **kwargs):
                self.call_count += 1
                # Every other call returns garbage to simulate isolated
                # parse failures.
                text = good if self.call_count % 2 == 1 else "not json"
                return Completion(
                    text=text,
                    model="mock",
                    usage=TokenUsage(input_tokens=5, output_tokens=10),
                )

        llm = _AlternatingLLM()
        chunks = ["A", "B", "C", "D"]
        # Disable retries so we test single-attempt failure isolation.
        # (With retries enabled the alternating call_count pattern would
        # mask failures by re-rolling them.)
        facts = await extract_facts_verbatim_parallel(
            chunks, llm, max_retries=1,
        )
        # All 4 ExtractedFacts produced (failures fall through to
        # metadata-less ExtractedFact preserving chunk text).
        assert len(facts) == 4
        for i, fact in enumerate(facts):
            assert fact.what == chunks[i]
        # Every other one has good metadata; the rest have N/A defaults.
        assert facts[0].who == "Bob"
        assert facts[1].who == "N/A"
        assert facts[2].who == "Bob"
        assert facts[3].who == "N/A"

    @pytest.mark.asyncio
    async def test_retry_on_malformed_json(self):
        """Phase 4: parse-failure on first attempt → retry → success."""
        from astrocyte.pipeline.fact_extraction import (
            extract_facts_verbatim_parallel,
        )

        good = (
            '{"when":"now","where":"N/A","who":"Carol","why":"N/A",'
            '"fact_type":"experience","occurred_start":null,'
            '"occurred_end":null,"entities":[]}'
        )

        class _FailFirstLLM(MockLLMProvider):
            def __init__(self):
                super().__init__()
                self.call_count = 0

            async def complete(self, messages, **kwargs):
                self.call_count += 1
                # First call returns garbage, retries return good.
                text = "not json" if self.call_count == 1 else good
                return Completion(
                    text=text,
                    model="mock",
                    usage=TokenUsage(input_tokens=5, output_tokens=10),
                )

        llm = _FailFirstLLM()
        # Use base_retry_delay=0 to keep the test fast.
        facts = await extract_facts_verbatim_parallel(
            ["chunk-X"], llm, max_retries=3, base_retry_delay=0.0,
        )
        assert len(facts) == 1
        assert facts[0].who == "Carol", "retry path should have recovered"
        # Two LLM calls: first fails, second succeeds.
        assert llm.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhaustion_falls_through(self):
        """Phase 4: when all retries fail, return metadata-less fact."""
        from astrocyte.pipeline.fact_extraction import (
            extract_facts_verbatim_parallel,
        )

        llm = _ScriptedLLM("not json ever")
        facts = await extract_facts_verbatim_parallel(
            ["doomed-chunk"], llm, max_retries=3, base_retry_delay=0.0,
        )
        assert len(facts) == 1
        # Chunk text preserved, metadata defaults applied.
        assert facts[0].what == "doomed-chunk"
        assert facts[0].who == "N/A"
        # Three attempts before giving up.
        assert llm.call_count == 3

    @pytest.mark.asyncio
    async def test_empty_chunks_returns_empty(self):
        from astrocyte.pipeline.fact_extraction import (
            extract_facts_verbatim_parallel,
        )

        llm = _ScriptedLLM("{}")
        assert await extract_facts_verbatim_parallel([], llm) == []
        assert await extract_facts_verbatim_parallel(["", "  "], llm) == []
        assert llm.call_count == 0


class TestMaterializeFactsTextIsChunkVerbatim:
    """``materialize_facts`` always stores ``fact.what`` as the
    VectorItem text (the verbatim path is now the only path — concise
    decoration was removed in M9)."""

    def test_text_is_raw_chunk_not_decorated(self):
        facts = [
            ExtractedFact(
                what="Alice went hiking yesterday.",
                who="Alice", when="yesterday", where="N/A",
            ),
        ]
        materialized = materialize_facts(facts, bank_id="b1")
        # NOT "Alice went hiking yesterday. | Involving: Alice | When: yesterday"
        assert materialized.vector_items[0].text == "Alice went hiking yesterday."

    def test_structured_fields_still_promoted_to_metadata(self):
        """The verbatim path keeps the fact's structured dimensions in
        ``_fact_*`` metadata for downstream filter/rerank — only the
        VectorItem text is undecorated."""
        facts = [
            ExtractedFact(
                what="Alice went hiking yesterday.",
                who="Alice", when="yesterday",
            ),
        ]
        materialized = materialize_facts(facts, bank_id="b1")
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


class TestSFEConciseModeRejection:
    """The concise extraction_mode was removed in M9. validate_astrocyte_config
    must raise ConfigError with a pointer to the migration path."""

    def test_concise_mode_raises_with_migration_hint(self):
        from astrocyte.config import (
            AstrocyteConfig,
            ConfigError,
            validate_astrocyte_config,
        )

        cfg = AstrocyteConfig()
        cfg.structured_fact_extraction.extraction_mode = "concise"
        with pytest.raises(ConfigError, match="concise"):
            validate_astrocyte_config(cfg)

    def test_verbatim_mode_passes(self):
        from astrocyte.config import AstrocyteConfig, validate_astrocyte_config

        cfg = AstrocyteConfig()
        # Default and explicit should both validate cleanly.
        validate_astrocyte_config(cfg)
        cfg.structured_fact_extraction.extraction_mode = "verbatim"
        validate_astrocyte_config(cfg)


class TestSFEConfigWiring:
    """Phase 1+3 of cost-control port: ``StructuredFactExtractionConfig``
    fields (``chunk_max_size``, ``parallel_chunks``,
    ``parallel_chunks_max_concurrency``) must flow through to the
    orchestrator pipeline attributes so the SFE retain path actually
    honors them at runtime.

    This is a guard against silent misconfiguration: if the wiring step
    in ``Astrocyte.set_pipeline`` ever drops one of these fields, the
    feature will be a no-op even when the YAML sets it.
    """

    def _make_brain_with_pipeline(self, config):
        from astrocyte._astrocyte import Astrocyte
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator
        from astrocyte.testing.in_memory import (
            InMemoryVectorStore,
            MockLLMProvider,
        )

        brain = Astrocyte(config)
        pipeline = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
        )
        brain.set_pipeline(pipeline)
        return pipeline

    def test_chunk_max_size_default_is_none(self):
        from astrocyte.config import AstrocyteConfig

        config = AstrocyteConfig()
        # Defaults: SFE off, no chunk_max_size override, parallel disabled.
        pipeline = self._make_brain_with_pipeline(config)
        assert pipeline.structured_fact_extraction_chunk_max_size is None
        assert pipeline.structured_fact_extraction_parallel_chunks is False
        assert (
            pipeline.structured_fact_extraction_parallel_chunks_max_concurrency
            == 6
        )

    def test_chunk_max_size_overrides_propagate(self):
        from astrocyte.config import AstrocyteConfig

        config = AstrocyteConfig()
        config.structured_fact_extraction.enabled = True
        config.structured_fact_extraction.chunk_max_size = 2048
        config.structured_fact_extraction.parallel_chunks = True
        config.structured_fact_extraction.parallel_chunks_max_concurrency = 8

        pipeline = self._make_brain_with_pipeline(config)
        assert pipeline.structured_fact_extraction_enabled is True
        assert pipeline.structured_fact_extraction_chunk_max_size == 2048
        assert pipeline.structured_fact_extraction_parallel_chunks is True
        assert (
            pipeline.structured_fact_extraction_parallel_chunks_max_concurrency
            == 8
        )

    @pytest.mark.asyncio
    async def test_chunk_max_size_used_in_pre_chunking(self, monkeypatch):
        """End-to-end check: when SFE retains text, the configured
        ``chunk_max_size`` is the value passed into ``chunk_text`` —
        not the orchestrator's default chunking decision.
        """
        from astrocyte._astrocyte import Astrocyte
        from astrocyte.config import AstrocyteConfig
        from astrocyte.pipeline import chunking as chunking_mod

        captured_max_size: list[int | None] = []
        original_chunk_text = chunking_mod.chunk_text

        # Spy on the module-level chunk_text. The SFE path inside
        # ``_structured_fact_extraction_for_text`` does a function-local
        # ``from astrocyte.pipeline.chunking import chunk_text`` so it
        # picks up our patched symbol on each call.
        def _spy_chunk_text(text, *, strategy, max_chunk_size=512, **kw):
            captured_max_size.append(max_chunk_size)
            return original_chunk_text(
                text, strategy=strategy, max_chunk_size=max_chunk_size, **kw,
            )

        monkeypatch.setattr(chunking_mod, "chunk_text", _spy_chunk_text)

        config = AstrocyteConfig()
        config.provider_tier = "storage"
        config.barriers.pii.mode = "disabled"
        config.escalation.degraded_mode = "error"
        config.structured_fact_extraction.enabled = True
        config.structured_fact_extraction.chunk_max_size = 1024

        pipeline = self._make_brain_with_pipeline(config)

        # Drive a retain so the SFE pre-chunking path fires. The
        # MockLLM's canned response won't satisfy the verbatim schema
        # which is fine — the assertion only cares about the
        # chunk_max_size that was forwarded.
        brain = Astrocyte(config)
        brain.set_pipeline(pipeline)
        await brain.retain(
            "Paragraph one.\n\nParagraph two.\n\nParagraph three.",
            bank_id="bank-test",
        )

        assert captured_max_size, "chunk_text was never called by SFE path"
        assert 1024 in captured_max_size, (
            f"SFE did not use chunk_max_size=1024; saw {captured_max_size!r}"
        )
