"""M3 — PipelineOrchestrator uses extraction profile resolution (TDD)."""

from __future__ import annotations

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig, ExtractionProfileConfig
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryGraphStore, InMemoryVectorStore, MockLLMProvider
from astrocyte.types import RetainRequest


@pytest.mark.asyncio
class TestOrchestratorM3Chunking:
    async def test_document_content_type_uses_paragraph_chunks(self):
        vs = InMemoryVectorStore()
        # Small max so two paragraphs cannot merge into one chunk
        pipeline = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=MockLLMProvider(),
            chunk_strategy="sentence",
            max_chunk_size=40,
        )
        content = "First paragraph text here.\n\nSecond paragraph text there."
        req = RetainRequest(content=content, bank_id="b1", content_type="document")
        result = await pipeline.retain(req)
        assert result.stored is True
        assert len(vs._vectors) == 2

    async def test_text_content_type_unchanged_single_chunk_for_short_input(self):
        vs = InMemoryVectorStore()
        pipeline = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=MockLLMProvider(),
            chunk_strategy="sentence",
            max_chunk_size=512,
        )
        req = RetainRequest(content="One short sentence.", bank_id="b1", content_type="text")
        result = await pipeline.retain(req)
        assert result.stored is True
        assert len(vs._vectors) == 1

    async def test_extraction_profile_overrides_to_paragraph(self):
        vs = InMemoryVectorStore()
        profiles = {"notes": ExtractionProfileConfig(chunking_strategy="paragraph")}
        pipeline = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=MockLLMProvider(),
            chunk_strategy="sentence",
            max_chunk_size=24,
            extraction_profiles=profiles,
        )
        content = "Line one text.\n\nLine two text."
        req = RetainRequest(
            content=content,
            bank_id="b1",
            content_type="conversation",
            extraction_profile="notes",
        )
        result = await pipeline.retain(req)
        assert result.stored is True
        assert len(vs._vectors) == 2

    async def test_unknown_extraction_profile_ignored(self):
        vs = InMemoryVectorStore()
        pipeline = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=MockLLMProvider(),
            chunk_strategy="sentence",
            max_chunk_size=512,
            extraction_profiles={},
        )
        req = RetainRequest(
            content="Alice: hi\nBob: there",
            bank_id="b1",
            content_type="conversation",
            extraction_profile="missing",
        )
        result = await pipeline.retain(req)
        assert result.stored is True
        # dialogue path → typically 1 chunk for two short turns
        assert len(vs._vectors) >= 1


class TestSetPipelineWiresExtractionProfiles:
    def test_set_pipeline_copies_profiles_from_astrocyte_config(self):
        cfg = AstrocyteConfig()
        cfg.provider_tier = "storage"
        cfg.barriers.pii.mode = "disabled"
        cfg.escalation.degraded_mode = "error"
        cfg.extraction_profiles = {"mine": ExtractionProfileConfig(chunking_strategy="paragraph")}
        brain = Astrocyte(cfg)
        pipeline = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
        )
        assert pipeline.extraction_profiles is None
        brain.set_pipeline(pipeline)
        assert pipeline.extraction_profiles is not None
        assert "mine" in pipeline.extraction_profiles

    async def test_entity_extraction_disabled_by_profile_skips_llm_entity_call(self):
        vs = InMemoryVectorStore()
        gs = InMemoryGraphStore()
        llm = MockLLMProvider()
        profiles = {"no_ent": ExtractionProfileConfig(entity_extraction=False)}
        pipeline = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=llm,
            graph_store=gs,
            chunk_strategy="sentence",
            max_chunk_size=256,
            extraction_profiles=profiles,
        )
        req = RetainRequest(
            content="Some text about nothing.",
            bank_id="b1",
            content_type="text",
            extraction_profile="no_ent",
        )
        await pipeline.retain(req)
        assert llm._call_count == 0

    async def test_metadata_entity_extraction_skips_llm_and_stores_metadata_entities(self):
        vs = InMemoryVectorStore()
        gs = InMemoryGraphStore()
        llm = MockLLMProvider()
        profiles = {"metadata_ent": ExtractionProfileConfig(entity_extraction="metadata")}
        pipeline = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=llm,
            graph_store=gs,
            chunk_strategy="sentence",
            max_chunk_size=256,
            extraction_profiles=profiles,
        )
        req = RetainRequest(
            content="Alice discussed planets with Bob.",
            bank_id="b1",
            metadata={"locomo_persons": "Alice,Bob"},
            content_type="conversation",
            extraction_profile="metadata_ent",
        )

        await pipeline.retain(req)

        assert llm._call_count == 0
        assert {entity.name for entity in gs._entities["b1"].values()} == {"Alice", "Bob"}

    async def test_retain_many_batches_embeddings_and_uses_metadata_entities(self):
        class CountingEmbedLLM(MockLLMProvider):
            def __init__(self) -> None:
                super().__init__()
                self.embed_calls = 0
                self.embed_batch_sizes: list[int] = []

            async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
                self.embed_calls += 1
                self.embed_batch_sizes.append(len(texts))
                return await super().embed(texts, model=model)

        vs = InMemoryVectorStore()
        gs = InMemoryGraphStore()
        llm = CountingEmbedLLM()
        profiles = {"metadata_ent": ExtractionProfileConfig(entity_extraction="metadata")}
        pipeline = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=llm,
            graph_store=gs,
            chunk_strategy="sentence",
            max_chunk_size=256,
            extraction_profiles=profiles,
        )
        requests = [
            RetainRequest(
                content="Alice discussed planets with Bob.",
                bank_id="b1",
                metadata={"locomo_persons": "Alice,Bob"},
                content_type="conversation",
                extraction_profile="metadata_ent",
            ),
            RetainRequest(
                content="Carol discussed rockets with Dana.",
                bank_id="b1",
                metadata={"locomo_persons": "Carol,Dana"},
                content_type="conversation",
                extraction_profile="metadata_ent",
            ),
        ]

        results = await pipeline.retain_many(requests)

        assert [result.stored for result in results] == [True, True]
        assert llm.embed_calls == 1
        assert llm.embed_batch_sizes == [2]
        assert {entity.name for entity in gs._entities["b1"].values()} == {"Alice", "Bob", "Carol", "Dana"}

    async def test_extraction_profile_metadata_mapping_on_stored_vectors(self):
        vs = InMemoryVectorStore()
        profiles = {
            "mapped": ExtractionProfileConfig(
                metadata_mapping={"who": "$.name"},
                tag_rules=[{"contains": "Ada", "tags": ["greeting"]}],
            ),
        }
        pipeline = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=MockLLMProvider(),
            chunk_strategy="sentence",
            max_chunk_size=512,
            extraction_profiles=profiles,
        )
        req = RetainRequest(
            content='{"name": "Ada", "note": "hello"}',
            bank_id="b1",
            content_type="text",
            extraction_profile="mapped",
        )
        result = await pipeline.retain(req)
        assert result.stored is True
        stored = next(iter(vs._vectors.values()))
        assert stored.metadata is not None
        assert stored.metadata.get("who") == "Ada"
        assert stored.tags is not None and "greeting" in stored.tags

    async def test_extraction_profile_fact_type_on_stored_vectors(self):
        vs = InMemoryVectorStore()
        profiles = {"obs": ExtractionProfileConfig(fact_type="observation", chunking_strategy="sentence")}
        pipeline = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=MockLLMProvider(),
            chunk_strategy="sentence",
            max_chunk_size=512,
            extraction_profiles=profiles,
        )
        req = RetainRequest(
            content="A short fact.",
            bank_id="b1",
            content_type="text",
            extraction_profile="obs",
        )
        await pipeline.retain(req)
        stored = next(iter(vs._vectors.values()))
        assert stored.fact_type == "observation"

    async def test_builtin_extraction_profile_resolves_without_explicit_yaml(self):
        vs = InMemoryVectorStore()
        pipeline = PipelineOrchestrator(
            vector_store=vs,
            llm_provider=MockLLMProvider(),
            chunk_strategy="paragraph",
            max_chunk_size=512,
            extraction_profiles={},
        )
        req = RetainRequest(
            content="One line.",
            bank_id="b1",
            content_type="conversation",
            extraction_profile="builtin_text",
        )
        result = await pipeline.retain(req)
        assert result.stored is True
        assert len(vs._vectors) == 1
        stored = next(iter(vs._vectors.values()))
        assert stored.text == "One line."
