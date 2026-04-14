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


# ---------------------------------------------------------------------------
# Per-bank rerank resolution at recall time (Phase 2, Step 8b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMipRerankResolution:
    async def test_recall_applies_per_bank_rerank_spec(self):
        """When a rule targets the recall bank with a RerankSpec, scores reflect the override."""
        from astrocyte.mip.router import MipRouter
        from astrocyte.mip.schema import (
            ActionSpec,
            MatchBlock,
            MatchSpec,
            MipConfig,
            PipelineSpec,
            RerankSpec,
            RoutingRule,
        )
        from astrocyte.types import RecallRequest

        pipeline, vs = _orchestrator()

        # Seed two memories: one matches the query terms, one does not
        await pipeline.retain(RetainRequest(content="dark mode preference here.", bank_id="b1"))
        await pipeline.retain(RetainRequest(content="completely unrelated topic.", bank_id="b1"))

        # Record baseline recall — default rerank weights
        baseline = await pipeline.recall(RecallRequest(query="dark mode", bank_id="b1", max_results=2))
        baseline_top_score = baseline.hits[0].score

        # Now attach a router with a rule that bumps keyword_weight aggressively for bank b1
        rule = RoutingRule(
            name="b1-strong-keywords",
            priority=10,
            match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="text")]),
            action=ActionSpec(
                bank="b1",
                pipeline=PipelineSpec(version=1, rerank=RerankSpec(keyword_weight=1.0)),
            ),
        )
        pipeline.mip_router = MipRouter(MipConfig(rules=[rule]))

        boosted = await pipeline.recall(RecallRequest(query="dark mode", bank_id="b1", max_results=2))
        # Top hit's score should be measurably higher with the override applied
        assert boosted.hits[0].score > baseline_top_score

    async def test_recall_unchanged_when_no_router_attached(self):
        from astrocyte.types import RecallRequest

        pipeline, vs = _orchestrator()
        await pipeline.retain(RetainRequest(content="dark mode preference.", bank_id="b1"))

        # mip_router is None by default → behavior identical to pre-Step 8b
        result = await pipeline.recall(RecallRequest(query="dark mode", bank_id="b1", max_results=1))
        assert pipeline.mip_router is None
        assert len(result.hits) == 1

    async def test_recall_unchanged_when_router_has_no_rule_for_bank(self):
        from astrocyte.mip.router import MipRouter
        from astrocyte.mip.schema import (
            ActionSpec,
            MatchBlock,
            MatchSpec,
            MipConfig,
            PipelineSpec,
            RerankSpec,
            RoutingRule,
        )
        from astrocyte.types import RecallRequest

        pipeline, vs = _orchestrator()
        await pipeline.retain(RetainRequest(content="dark mode preference.", bank_id="b1"))
        baseline = await pipeline.recall(RecallRequest(query="dark mode", bank_id="b1", max_results=1))

        # Router with a rule targeting a different bank should not affect b1 recall
        rule = RoutingRule(
            name="other-bank",
            priority=10,
            match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="text")]),
            action=ActionSpec(
                bank="other-bank",
                pipeline=PipelineSpec(version=1, rerank=RerankSpec(keyword_weight=1.0)),
            ),
        )
        pipeline.mip_router = MipRouter(MipConfig(rules=[rule]))
        unchanged = await pipeline.recall(RecallRequest(query="dark mode", bank_id="b1", max_results=1))
        assert unchanged.hits[0].score == baseline.hits[0].score


# ---------------------------------------------------------------------------
# Version-drift warning at recall (Phase 2, Step 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMipVersionDriftWarning:
    async def test_warns_when_hit_version_differs_from_current(self, caplog):
        import logging

        from astrocyte.mip.router import MipRouter
        from astrocyte.mip.schema import (
            ActionSpec,
            MatchBlock,
            MatchSpec,
            MipConfig,
            PipelineSpec,
            RoutingRule,
        )
        from astrocyte.types import RecallRequest

        pipeline, vs = _orchestrator()

        # Retain a memory under MIP pipeline version 1
        await pipeline.retain(
            RetainRequest(
                content="dark mode preference here.",
                bank_id="b1",
                mip_pipeline=PipelineSpec(version=1),
                mip_rule_name="b1-rule",
            ),
        )

        # Now wire a router whose rule for bank b1 advertises version 2 (drift)
        rule = RoutingRule(
            name="b1-rule",
            priority=10,
            match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="text")]),
            action=ActionSpec(bank="b1", pipeline=PipelineSpec(version=2)),
        )
        pipeline.mip_router = MipRouter(MipConfig(rules=[rule]))

        with caplog.at_level(logging.WARNING, logger="astrocyte.mip"):
            await pipeline.recall(RecallRequest(query="dark mode", bank_id="b1", max_results=5))

        # Expect a single warning mentioning current version, drifted version, and the bank
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("version drift" in r.message.lower() for r in warnings)
        msg = next(r.getMessage() for r in warnings if "drift" in r.getMessage().lower())
        assert "b1" in msg
        assert "current=2" in msg
        assert "[1]" in msg

    async def test_no_warning_when_versions_match(self, caplog):
        import logging

        from astrocyte.mip.router import MipRouter
        from astrocyte.mip.schema import (
            ActionSpec,
            MatchBlock,
            MatchSpec,
            MipConfig,
            PipelineSpec,
            RoutingRule,
        )
        from astrocyte.types import RecallRequest

        pipeline, vs = _orchestrator()
        await pipeline.retain(
            RetainRequest(
                content="dark mode preference here.",
                bank_id="b1",
                mip_pipeline=PipelineSpec(version=3),
                mip_rule_name="r",
            ),
        )

        rule = RoutingRule(
            name="r",
            priority=10,
            match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="text")]),
            action=ActionSpec(bank="b1", pipeline=PipelineSpec(version=3)),
        )
        pipeline.mip_router = MipRouter(MipConfig(rules=[rule]))

        with caplog.at_level(logging.WARNING, logger="astrocyte.mip"):
            await pipeline.recall(RecallRequest(query="dark mode", bank_id="b1", max_results=5))

        assert not any("drift" in r.getMessage().lower() for r in caplog.records)

    async def test_no_warning_when_no_router_attached(self, caplog):
        import logging

        from astrocyte.types import RecallRequest

        pipeline, vs = _orchestrator()
        await pipeline.retain(
            RetainRequest(
                content="dark mode preference.",
                bank_id="b1",
                mip_pipeline=PipelineSpec(version=99),
                mip_rule_name="r",
            ),
        )
        # No router → no resolved version → no comparison → no warning
        with caplog.at_level(logging.WARNING, logger="astrocyte.mip"):
            await pipeline.recall(RecallRequest(query="dark mode", bank_id="b1", max_results=5))
        assert not any("drift" in r.getMessage().lower() for r in caplog.records)

    async def test_hits_without_version_are_silently_ignored(self, caplog):
        import logging

        from astrocyte.mip.router import MipRouter
        from astrocyte.mip.schema import (
            ActionSpec,
            MatchBlock,
            MatchSpec,
            MipConfig,
            PipelineSpec,
            RoutingRule,
        )
        from astrocyte.types import RecallRequest

        pipeline, vs = _orchestrator()
        # Retain WITHOUT MIP — no _mip.pipeline_version on the chunk
        await pipeline.retain(RetainRequest(content="dark mode preference.", bank_id="b1"))

        rule = RoutingRule(
            name="r",
            priority=10,
            match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="text")]),
            action=ActionSpec(bank="b1", pipeline=PipelineSpec(version=5)),
        )
        pipeline.mip_router = MipRouter(MipConfig(rules=[rule]))

        with caplog.at_level(logging.WARNING, logger="astrocyte.mip"):
            await pipeline.recall(RecallRequest(query="dark mode", bank_id="b1", max_results=5))

        # No persisted version on the hit → nothing to compare → no warning
        assert not any("drift" in r.getMessage().lower() for r in caplog.records)
