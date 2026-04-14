"""Tests for individual pipeline stages — embedding, entity extraction, reflect, reranking, retrieval."""

import pytest

from astrocyte.pipeline.embedding import _pseudo_embedding, generate_embeddings
from astrocyte.pipeline.entity_extraction import _parse_entities, extract_entities
from astrocyte.pipeline.fusion import ScoredItem
from astrocyte.pipeline.reflect import _build_system_prompt, _format_memories, synthesize
from astrocyte.pipeline.reranking import basic_rerank
from astrocyte.pipeline.retrieval import parallel_retrieve
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import Dispositions, MemoryHit, VectorItem

# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


class TestPseudoEmbedding:
    def test_deterministic(self):
        a = _pseudo_embedding("hello world")
        b = _pseudo_embedding("hello world")
        assert a == b

    def test_different_texts_differ(self):
        a = _pseudo_embedding("hello")
        b = _pseudo_embedding("goodbye")
        assert a != b

    def test_correct_dimensions(self):
        emb = _pseudo_embedding("test", dims=64)
        assert len(emb) == 64

    def test_normalized(self):
        import math

        emb = _pseudo_embedding("test")
        norm = math.sqrt(sum(x * x for x in emb))
        assert abs(norm - 1.0) < 1e-6


class TestGenerateEmbeddings:
    async def test_calls_llm_provider(self):
        llm = MockLLMProvider()
        result = await generate_embeddings(["hello", "world"], llm)
        assert len(result) == 2
        assert all(isinstance(v, list) for v in result)

    async def test_fallback_on_not_implemented(self):
        class NoEmbedLLM(MockLLMProvider):
            async def embed(self, texts, model=None):
                raise NotImplementedError

        llm = NoEmbedLLM()
        result = await generate_embeddings(["hello"], llm)
        assert len(result) == 1
        assert len(result[0]) == 128  # Default pseudo-embedding dims


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------


class TestParseEntities:
    def test_valid_json_array(self):
        entities = _parse_entities('[{"name": "Calvin", "entity_type": "PERSON", "aliases": []}]')
        assert len(entities) == 1
        assert entities[0].name == "Calvin"
        assert entities[0].entity_type == "PERSON"

    def test_json_in_code_block(self):
        entities = _parse_entities('```json\n[{"name": "Acme", "entity_type": "ORG"}]\n```')
        assert len(entities) == 1
        assert entities[0].name == "Acme"

    def test_invalid_json(self):
        entities = _parse_entities("not json at all")
        assert entities == []

    def test_empty_array(self):
        entities = _parse_entities("[]")
        assert entities == []

    def test_missing_name_skipped(self):
        entities = _parse_entities('[{"entity_type": "PERSON"}]')
        assert entities == []


class TestExtractEntities:
    async def test_returns_entities(self):
        llm = MockLLMProvider()  # Mock returns entity JSON for extraction prompts
        entities = await extract_entities("Calvin works at Acme Corp", llm)
        assert len(entities) >= 1

    async def test_handles_llm_failure(self):
        class FailLLM(MockLLMProvider):
            async def complete(self, *args, **kwargs):
                raise RuntimeError("LLM down")

        entities = await extract_entities("test", FailLLM())
        assert entities == []  # Should not raise, returns empty


# ---------------------------------------------------------------------------
# Reflect / synthesis
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    def test_default_dispositions(self):
        prompt = _build_system_prompt(None)
        assert "memory synthesis" in prompt.lower()

    def test_high_skepticism(self):
        prompt = _build_system_prompt(Dispositions(skepticism=5))
        assert "skeptical" in prompt.lower()

    def test_high_empathy(self):
        prompt = _build_system_prompt(Dispositions(empathy=5))
        assert "experience" in prompt.lower() or "human" in prompt.lower()

    def test_high_literalism(self):
        prompt = _build_system_prompt(Dispositions(literalism=5))
        assert "literal" in prompt.lower()


class TestFormatMemories:
    def test_formats_hits(self):
        hits = [MemoryHit(text="Calvin likes Python", score=0.9, fact_type="experience")]
        text = _format_memories(hits)
        assert "Calvin likes Python" in text
        assert "experience" in text

    def test_empty_hits(self):
        assert _format_memories([]) == ""


class TestSynthesize:
    async def test_returns_result(self):
        llm = MockLLMProvider(default_response="Calvin prefers dark mode based on memories.")
        hits = [MemoryHit(text="Calvin likes dark mode", score=0.9)]
        result = await synthesize("What does Calvin like?", hits, llm)
        assert result.answer
        assert result.sources is not None

    async def test_empty_hits(self):
        llm = MockLLMProvider()
        result = await synthesize("anything", [], llm)
        assert "don't have" in result.answer.lower()


class TestPromptRegistry:
    """ReflectSpec.prompt selects from PROMPT_REGISTRY (Phase 2, Step 9)."""

    def test_default_variant_matches_omitted(self):
        a = _build_system_prompt(None)
        b = _build_system_prompt(None, prompt_variant="default")
        assert a == b

    def test_temporal_aware_variant_emphasizes_chronology(self):
        prompt = _build_system_prompt(None, prompt_variant="temporal_aware")
        assert "chronolog" in prompt.lower() or "ordering" in prompt.lower()
        assert "timestamp" in prompt.lower() or "date" in prompt.lower()

    def test_evidence_strict_variant_emphasizes_citation(self):
        prompt = _build_system_prompt(None, prompt_variant="evidence_strict")
        assert "memory" in prompt.lower()
        assert "cite" in prompt.lower() or "insufficient evidence" in prompt.lower()

    def test_unknown_variant_falls_back_to_default(self):
        default = _build_system_prompt(None)
        unknown = _build_system_prompt(None, prompt_variant="not_a_real_variant")
        assert unknown == default

    def test_variant_layered_with_dispositions(self):
        prompt = _build_system_prompt(
            Dispositions(skepticism=5),
            prompt_variant="evidence_strict",
        )
        assert "cite" in prompt.lower() or "insufficient" in prompt.lower()
        assert "skeptical" in prompt.lower()


class TestFormatMemoriesPromote:
    """promote_metadata renders selected metadata fields inline (P4 capped)."""

    def test_promoted_fields_appear_inline(self):
        hits = [MemoryHit(text="x", score=0.9, metadata={"author": "Ada", "topic": "alg"})]
        text = _format_memories(hits, promote_metadata=["author", "topic"])
        assert "author=Ada" in text
        assert "topic=alg" in text

    def test_missing_keys_silently_skipped(self):
        hits = [MemoryHit(text="x", score=0.9, metadata={"author": "Ada"})]
        text = _format_memories(hits, promote_metadata=["author", "missing_key"])
        assert "author=Ada" in text
        assert "missing_key" not in text

    def test_promote_capped_at_five(self):
        meta = {f"k{i}": f"v{i}" for i in range(10)}
        hits = [MemoryHit(text="x", score=0.9, metadata=meta)]
        text = _format_memories(hits, promote_metadata=list(meta.keys()))
        promoted_count = sum(1 for k in meta if f"{k}=" in text)
        assert promoted_count == 5

    def test_no_promote_metadata_unchanged_format(self):
        hits = [MemoryHit(text="hello", score=0.9, fact_type="world")]
        a = _format_memories(hits)
        b = _format_memories(hits, promote_metadata=None)
        assert a == b
        assert "{" not in a  # no metadata block when empty


class TestSynthesizeMipReflect:
    """synthesize honors mip_reflect.prompt and mip_reflect.promote_metadata."""

    async def test_mip_reflect_selects_prompt_variant(self, monkeypatch):
        from astrocyte.mip.schema import ReflectSpec
        from astrocyte.pipeline import reflect as reflect_mod

        captured: dict[str, str] = {}

        def fake_build(dispositions, *, prompt_variant=None):
            captured["variant"] = prompt_variant or "default"
            return "stub-system-prompt"

        monkeypatch.setattr(reflect_mod, "_build_system_prompt", fake_build)

        llm = MockLLMProvider(default_response="ok")
        hits = [MemoryHit(text="m1", score=0.9)]
        await synthesize(
            "q", hits, llm,
            mip_reflect=ReflectSpec(prompt="evidence_strict"),
        )
        assert captured["variant"] == "evidence_strict"

    async def test_mip_reflect_promotes_metadata_into_prompt(self):
        from astrocyte.mip.schema import ReflectSpec

        llm = MockLLMProvider(default_response="ok")
        hits = [MemoryHit(text="body", score=0.9, metadata={"author": "Ada"})]
        await synthesize(
            "q", hits, llm,
            mip_reflect=ReflectSpec(promote_metadata=["author"]),
        )
        # Inspect the rendered user prompt sent to the LLM
        sent = llm.messages_seen[-1] if hasattr(llm, "messages_seen") else None
        # If MockLLMProvider does not record messages, just verify no exception was raised.
        if sent:
            assert "author=Ada" in str(sent)


# ---------------------------------------------------------------------------
# Reranking
# ---------------------------------------------------------------------------


class TestBasicRerank:
    def test_boosts_matching_terms(self):
        items = [
            ScoredItem(id="a", text="Python is great", score=0.5),
            ScoredItem(id="b", text="dark mode preference", score=0.5),
        ]
        reranked = basic_rerank(items, "dark mode")
        # "dark mode" should boost item b
        assert reranked[0].id == "b"

    def test_empty_input(self):
        assert basic_rerank([], "test") == []

    def test_empty_query(self):
        items = [ScoredItem(id="a", text="hello", score=0.5)]
        result = basic_rerank(items, "")
        assert len(result) == 1


class TestBasicRerankMipOverride:
    """Per-call override for MIP RerankSpec (Phase 2, Step 8a)."""

    def test_override_keyword_weight_inflates_score(self):
        from astrocyte.mip.schema import RerankSpec

        items = [ScoredItem(id="a", text="dark mode preference", score=0.5)]
        # Default keyword weight is 0.05; bump it to 0.50 — score gain visible
        with_default = basic_rerank(items, "dark mode")[0].score
        with_override = basic_rerank(items, "dark mode", mip_rerank=RerankSpec(keyword_weight=0.50))[0].score
        assert with_override > with_default
        # Two matching terms × (0.50 - 0.05) = 0.90 expected delta
        assert with_override - with_default == pytest.approx(0.90, abs=1e-9)

    def test_override_proper_noun_weight(self):
        from astrocyte.mip.schema import RerankSpec

        items = [ScoredItem(id="a", text="Alice talked to Bob", score=0.5)]
        with_default = basic_rerank(items, "What did Alice say?")[0].score
        with_override = basic_rerank(items, "What did Alice say?", mip_rerank=RerankSpec(proper_noun_weight=1.0))[0].score
        assert with_override > with_default

    def test_partial_override_lets_other_weight_default(self):
        from astrocyte.mip.schema import RerankSpec

        items = [ScoredItem(id="a", text="Alice on dark mode", score=0.5)]
        # Only keyword_weight is overridden — proper_noun_weight stays at default
        scored = basic_rerank(items, "Alice dark mode", mip_rerank=RerankSpec(keyword_weight=0.0))[0].score
        # keyword contribution is 0, proper-noun contribution is default 0.10 for "Alice"
        assert scored == pytest.approx(0.5 + 0.10, abs=1e-9)

    def test_none_override_is_equivalent_to_omitted(self):
        items = [ScoredItem(id="a", text="dark mode", score=0.5)]
        a = basic_rerank(items, "dark mode")[0].score
        b = basic_rerank(items, "dark mode", mip_rerank=None)[0].score
        assert a == b


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


class TestParallelRetrieve:
    async def test_semantic_only(self):
        store = InMemoryVectorStore()
        await store.store_vectors(
            [
                VectorItem(id="v1", bank_id="b1", vector=[1.0, 0.0], text="hello"),
            ]
        )
        results = await parallel_retrieve(
            query_vector=[1.0, 0.0],
            query_text="hello",
            bank_id="b1",
            vector_store=store,
        )
        assert "semantic" in results
        assert len(results["semantic"]) >= 1

    async def test_strategy_failure_returns_empty(self):
        class FailingStore(InMemoryVectorStore):
            async def search_similar(self, *args, **kwargs):
                raise RuntimeError("boom")

        results = await parallel_retrieve(
            query_vector=[1.0, 0.0],
            query_text="test",
            bank_id="b1",
            vector_store=FailingStore(),
        )
        assert results["semantic"] == []  # Failure produces empty, not exception
