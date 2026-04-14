"""MIP pipeline overrides plumbed through retain (Phase 1, Step 6).

Verifies that ``RetainRequest.mip_pipeline`` is honored by the orchestrator:
- ``chunker`` overrides influence chunk count / strategy
- ``dedup.threshold`` overrides influence per-chunk dedup
- ``dedup.action`` controls whether duplicates skip a chunk or the whole call
- ``_mip.rule`` and ``_mip.pipeline_version`` are persisted on chunk metadata
"""

from __future__ import annotations

import pytest

from astrocyte.mip.schema import ChunkerSpec, DedupSpec, PipelineSpec
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import RetainRequest


def _orchestrator(max_chunk_size: int = 512) -> tuple[PipelineOrchestrator, InMemoryVectorStore]:
    vs = InMemoryVectorStore()
    pipeline = PipelineOrchestrator(
        vector_store=vs,
        llm_provider=MockLLMProvider(),
        chunk_strategy="sentence",
        max_chunk_size=max_chunk_size,
    )
    return pipeline, vs


@pytest.mark.asyncio
class TestMipChunkerOverride:
    async def test_mip_chunker_strategy_overrides_content_type(self):
        """A `text` content_type with a MIP chunker.strategy=paragraph should chunk by paragraphs."""
        pipeline, vs = _orchestrator(max_chunk_size=40)
        content = "First paragraph here.\n\nSecond paragraph there."
        req = RetainRequest(
            content=content,
            bank_id="b1",
            content_type="text",
            mip_pipeline=PipelineSpec(version=1, chunker=ChunkerSpec(strategy="paragraph")),
            mip_rule_name="r1",
        )
        result = await pipeline.retain(req)
        assert result.stored is True
        # Paragraph splitter with small max yields two chunks here.
        assert len(vs._vectors) == 2

    async def test_no_mip_pipeline_preserves_existing_behavior(self):
        """When mip_pipeline is None, behavior is identical to pre-Step 6 retain."""
        pipeline, vs = _orchestrator(max_chunk_size=512)
        req = RetainRequest(content="One short sentence.", bank_id="b1", content_type="text")
        result = await pipeline.retain(req)
        assert result.stored is True
        assert len(vs._vectors) == 1


@pytest.mark.asyncio
class TestMipDedupOverride:
    async def test_dedup_threshold_override_treats_near_dup_as_duplicate(self):
        """Lowering dedup threshold via MIP catches near-duplicates the default would miss."""
        pipeline, vs = _orchestrator()
        # First call: no override, both chunks stored.
        await pipeline.retain(
            RetainRequest(content="Alpha sentence content here.", bank_id="b1", content_type="text"),
        )
        baseline = len(vs._vectors)
        assert baseline == 1

        # Second call with a very low threshold via MIP — same content is treated as dup
        # and the call should be deduplicated entirely (skip_chunk drops the only chunk).
        result = await pipeline.retain(
            RetainRequest(
                content="Alpha sentence content here.",
                bank_id="b1",
                content_type="text",
                mip_pipeline=PipelineSpec(version=1, dedup=DedupSpec(threshold=0.5)),
            ),
        )
        assert result.stored is False
        assert result.deduplicated is True
        assert len(vs._vectors) == baseline  # No new vector

    async def test_dedup_action_skip_rejects_whole_retain_on_any_duplicate(self):
        """``action: skip`` rejects the entire retain when ANY chunk matches."""
        pipeline, vs = _orchestrator(max_chunk_size=40)
        # Seed bank with a chunk
        await pipeline.retain(RetainRequest(content="Seed sentence one.", bank_id="b1"))
        # Now retain content where one sentence is a near-dup of the seed
        req = RetainRequest(
            content="Seed sentence one. Brand new distinct sentence two.",
            bank_id="b1",
            mip_pipeline=PipelineSpec(version=1, dedup=DedupSpec(action="skip")),
        )
        result = await pipeline.retain(req)
        # The "Seed sentence one." chunk is a duplicate, so the entire retain is rejected.
        assert result.stored is False
        assert result.deduplicated is True

    async def test_dedup_action_warn_keeps_duplicate_chunks(self):
        """``action: warn`` stores all chunks even if some are duplicates."""
        pipeline, vs = _orchestrator(max_chunk_size=40)
        await pipeline.retain(RetainRequest(content="Seed sentence one.", bank_id="b1"))
        before = len(vs._vectors)

        req = RetainRequest(
            content="Seed sentence one.",
            bank_id="b1",
            mip_pipeline=PipelineSpec(version=1, dedup=DedupSpec(action="warn")),
        )
        result = await pipeline.retain(req)
        assert result.stored is True
        assert len(vs._vectors) == before + 1


@pytest.mark.asyncio
class TestMipProvenancePersistence:
    async def test_rule_and_version_written_to_chunk_metadata(self):
        pipeline, vs = _orchestrator()
        req = RetainRequest(
            content="Hello world.",
            bank_id="b1",
            mip_pipeline=PipelineSpec(version=7),
            mip_rule_name="my-rule",
        )
        result = await pipeline.retain(req)
        assert result.stored is True
        stored = next(iter(vs._vectors.values()))
        assert stored.metadata is not None
        assert stored.metadata.get("_mip.rule") == "my-rule"
        assert stored.metadata.get("_mip.pipeline_version") == 7

    async def test_no_mip_metadata_when_routing_decision_absent(self):
        pipeline, vs = _orchestrator()
        req = RetainRequest(content="Hello world.", bank_id="b1")
        result = await pipeline.retain(req)
        assert result.stored is True
        stored = next(iter(vs._vectors.values()))
        # Either metadata is None or it does not contain _mip.* keys
        if stored.metadata is not None:
            assert "_mip.rule" not in stored.metadata
            assert "_mip.pipeline_version" not in stored.metadata

    async def test_rule_written_without_version(self):
        pipeline, vs = _orchestrator()
        req = RetainRequest(
            content="Hello world.",
            bank_id="b1",
            mip_pipeline=PipelineSpec(),
            mip_rule_name="r2",
        )
        result = await pipeline.retain(req)
        assert result.stored is True
        meta = next(iter(vs._vectors.values())).metadata or {}
        assert meta.get("_mip.rule") == "r2"
        assert "_mip.pipeline_version" not in meta
