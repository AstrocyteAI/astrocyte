"""M7 — recall authority in reflect synthesis and retain metadata producers."""

from __future__ import annotations

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig, ExtractionProfileConfig, RecallAuthorityConfig, RecallAuthorityTierConfig
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import RetainRequest


@pytest.mark.asyncio
async def test_reflect_prepends_authority_context_when_enabled() -> None:
    cfg = AstrocyteConfig()
    cfg.barriers.pii.mode = "disabled"
    cfg.recall_authority = RecallAuthorityConfig(
        enabled=True,
        rules_inline="Trust tier order.",
        apply_to_reflect=True,
        tier_by_bank={"b-auth": "t1"},
        tiers=[RecallAuthorityTierConfig(id="t1", priority=1, label="Primary")],
    )
    vs = InMemoryVectorStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=vs, llm_provider=llm)
    brain = Astrocyte(cfg)
    brain.set_pipeline(pipeline)

    await brain.retain("Omega is the last letter.", bank_id="b-auth")
    await brain.reflect("What is omega?", bank_id="b-auth")

    assert llm.last_user_message is not None
    assert "<authority_context>" in llm.last_user_message
    assert "Trust tier order." in llm.last_user_message
    assert "<memories>" in llm.last_user_message


@pytest.mark.asyncio
async def test_reflect_skips_authority_block_when_apply_to_reflect_false() -> None:
    cfg = AstrocyteConfig()
    cfg.barriers.pii.mode = "disabled"
    cfg.recall_authority = RecallAuthorityConfig(
        enabled=True,
        rules_inline="Should not appear in reflect.",
        apply_to_reflect=False,
        tier_by_bank={"b2": "t1"},
        tiers=[RecallAuthorityTierConfig(id="t1", priority=1, label="T")],
    )
    vs = InMemoryVectorStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=vs, llm_provider=llm)
    brain = Astrocyte(cfg)
    brain.set_pipeline(pipeline)

    await brain.retain("Fact one.", bank_id="b2")
    await brain.reflect("Fact?", bank_id="b2")

    assert llm.last_user_message is not None
    assert "<authority_context>" not in llm.last_user_message
    assert "Should not appear" not in llm.last_user_message


@pytest.mark.asyncio
async def test_tier_by_bank_sets_metadata_on_retain() -> None:
    ra = RecallAuthorityConfig(
        enabled=True,
        tier_by_bank={"bank-a": "canonical"},
        tiers=[RecallAuthorityTierConfig(id="canonical", priority=1, label="C")],
    )
    vs = InMemoryVectorStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=vs, llm_provider=llm)
    pipeline.recall_authority = ra

    await pipeline.retain(RetainRequest(content="Hello world text.", bank_id="bank-a"))

    assert len(vs._vectors) == 1
    vid = next(iter(vs._vectors))
    assert vs._vectors[vid].metadata is not None
    assert vs._vectors[vid].metadata.get("authority_tier") == "canonical"


@pytest.mark.asyncio
async def test_extraction_profile_authority_tier_overrides_bank_map() -> None:
    ra = RecallAuthorityConfig(
        enabled=True,
        tier_by_bank={"bank-b": "low"},
        tiers=[
            RecallAuthorityTierConfig(id="low", priority=2, label="L"),
            RecallAuthorityTierConfig(id="high", priority=1, label="H"),
        ],
    )
    profiles = {"vip": ExtractionProfileConfig(authority_tier="high")}
    vs = InMemoryVectorStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=vs, llm_provider=llm, extraction_profiles=profiles)
    pipeline.recall_authority = ra

    await pipeline.retain(
        RetainRequest(content="Override tier content.", bank_id="bank-b", extraction_profile="vip"),
    )

    vid = next(iter(vs._vectors))
    assert vs._vectors[vid].metadata.get("authority_tier") == "high"
