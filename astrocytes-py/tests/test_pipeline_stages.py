"""Tests for individual pipeline stages — embedding, entity extraction, reflect, reranking, retrieval."""

from astrocytes.pipeline.embedding import _pseudo_embedding, generate_embeddings
from astrocytes.pipeline.entity_extraction import _parse_entities, extract_entities
from astrocytes.pipeline.fusion import ScoredItem
from astrocytes.pipeline.reflect import _build_system_prompt, _format_memories, synthesize
from astrocytes.pipeline.reranking import basic_rerank
from astrocytes.pipeline.retrieval import parallel_retrieve
from astrocytes.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocytes.types import Dispositions, MemoryHit, VectorItem

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
