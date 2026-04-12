"""Tests for Phase 2 innovations — Tiered Retrieval, Curated Retain, Curated Recall,
Progressive Retrieval, Cross-Source Fusion, Cross-Engine Routing."""

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.hybrid import AdaptiveRouter
from astrocyte.pipeline.curated_recall import curate_recall_hits
from astrocyte.pipeline.curated_retain import _parse_curation_response
from astrocyte.pipeline.recall_cache import RecallCache
from astrocyte.pipeline.tiered_retrieval import TieredRetriever
from astrocyte.testing.in_memory import InMemoryEngineProvider, InMemoryVectorStore, MockLLMProvider
from astrocyte.types import EngineCapabilities, MemoryHit, RecallRequest, RecallResult


def _make_brain() -> tuple[Astrocyte, InMemoryEngineProvider]:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


# ---------------------------------------------------------------------------
# 2.1 Tiered Retrieval
# ---------------------------------------------------------------------------


class TestTieredRetrieval:
    async def test_cache_hit_returns_tier_0(self):
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator

        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        pipeline = PipelineOrchestrator(vector_store=vs, llm_provider=llm)
        cache = RecallCache(similarity_threshold=0.9)

        # Pre-populate cache
        cached_result = RecallResult(
            hits=[MemoryHit(text="cached memory", score=0.9)],
            total_available=1,
            truncated=False,
        )
        # Compute embedding using the same provider the retriever will use
        vecs = await llm.embed(["test query"])
        cache.put("bank-1", vecs[0], cached_result)

        tiered = TieredRetriever(pipeline, recall_cache=cache, max_tier=4)
        result = await tiered.retrieve(RecallRequest(query="test query", bank_id="bank-1"))

        assert result.trace is not None
        assert result.trace.tier_used == 0
        assert result.trace.cache_hit is True
        assert result.hits[0].text == "cached memory"

    async def test_tier_3_full_retrieval(self):
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator

        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        pipeline = PipelineOrchestrator(vector_store=vs, llm_provider=llm)

        # Store some content
        await pipeline.retain(
            __import__("astrocyte.types", fromlist=["RetainRequest"]).RetainRequest(
                content="Dark mode is Calvin's preference",
                bank_id="bank-1",
            )
        )

        tiered = TieredRetriever(pipeline, max_tier=3)
        result = await tiered.retrieve(RecallRequest(query="dark mode", bank_id="bank-1"))

        assert result.trace is not None
        assert result.trace.tier_used == 3

    async def test_max_tier_respected(self):
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator

        vs = InMemoryVectorStore()
        llm = MockLLMProvider()
        pipeline = PipelineOrchestrator(vector_store=vs, llm_provider=llm)

        tiered = TieredRetriever(pipeline, max_tier=0)
        result = await tiered.retrieve(RecallRequest(query="anything", bank_id="bank-1"))
        # With max_tier=0 and no cache, should return empty
        assert result.hits == []


# ---------------------------------------------------------------------------
# 2.2 Curated Retain
# ---------------------------------------------------------------------------


class TestCuratedRetain:
    def test_parse_valid_json(self):
        response = '{"action": "add", "content": "processed", "memory_layer": "fact", "reasoning": "new info"}'
        decision = _parse_curation_response(response, "original", set())
        assert decision.action == "add"
        assert decision.content == "original"  # Original content preserved, not LLM-rewritten
        assert decision.memory_layer == "fact"

    def test_parse_json_in_code_block(self):
        response = '```json\n{"action": "merge", "content": "merged", "memory_layer": "observation", "reasoning": "similar to existing", "merge_target_id": "m123"}\n```'
        decision = _parse_curation_response(response, "original", {"m123"})
        assert decision.action == "merge"
        assert decision.merge_target_id == "m123"

    def test_parse_invalid_json_falls_back(self):
        decision = _parse_curation_response("not json at all", "original", set())
        assert decision.action == "add"
        assert decision.content == "original"
        assert decision.memory_layer == "fact"

    def test_parse_invalid_action_normalized(self):
        response = '{"action": "UNKNOWN", "content": "test", "memory_layer": "fact", "reasoning": ""}'
        decision = _parse_curation_response(response, "original", set())
        assert decision.action == "add"  # Unknown → default to add

    def test_parse_invalid_layer_normalized(self):
        response = '{"action": "add", "content": "test", "memory_layer": "INVALID", "reasoning": ""}'
        decision = _parse_curation_response(response, "original", set())
        assert decision.memory_layer == "fact"  # Invalid → default to fact

    def test_skip_action(self):
        response = '{"action": "skip", "content": "", "memory_layer": "fact", "reasoning": "redundant"}'
        decision = _parse_curation_response(response, "original", set())
        assert decision.action == "skip"

    def test_delete_action_with_valid_target(self):
        response = '{"action": "delete", "content": "", "memory_layer": "fact", "reasoning": "contradicts", "merge_target_id": "m456"}'
        decision = _parse_curation_response(response, "original", {"m456"})
        assert decision.action == "delete"
        assert decision.merge_target_id == "m456"

    def test_delete_action_with_invalid_target_falls_back(self):
        """Destructive action referencing unknown memory ID should fall back to ADD."""
        response = '{"action": "delete", "content": "", "memory_layer": "fact", "reasoning": "contradicts", "merge_target_id": "unknown-id"}'
        decision = _parse_curation_response(response, "original", {"m456"})
        assert decision.action == "add"
        assert decision.merge_target_id is None

    def test_update_action(self):
        response = '{"action": "update", "content": "updated text", "memory_layer": "observation", "reasoning": "more accurate", "merge_target_id": "m789"}'
        decision = _parse_curation_response(response, "original", {"m789"})
        assert decision.action == "update"
        assert decision.memory_layer == "observation"


# ---------------------------------------------------------------------------
# 2.3 Curated Recall
# ---------------------------------------------------------------------------


class TestCuratedRecall:
    def test_freshness_boosts_recent(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        recent = MemoryHit(text="recent", score=0.5, occurred_at=now - timedelta(hours=1))
        old = MemoryHit(text="old", score=0.5, occurred_at=now - timedelta(days=60))

        curated = curate_recall_hits([old, recent])
        assert curated[0].text == "recent"  # Recent should rank higher

    def test_model_layer_boosted(self):
        fact = MemoryHit(text="fact", score=0.5, memory_layer="fact")
        model = MemoryHit(text="model", score=0.5, memory_layer="model")

        curated = curate_recall_hits([fact, model])
        assert curated[0].text == "model"

    def test_min_score_filters(self):
        low = MemoryHit(text="low", score=0.1)
        high = MemoryHit(text="high", score=0.9)

        curated = curate_recall_hits([low, high], min_score=0.4)
        assert len(curated) == 1
        assert curated[0].text == "high"

    def test_empty_input(self):
        assert curate_recall_hits([]) == []

    def test_no_occurred_at_gets_neutral_freshness(self):
        hit = MemoryHit(text="no date", score=0.5)
        curated = curate_recall_hits([hit])
        assert len(curated) == 1
        assert curated[0].score > 0

    def test_experience_type_reliable(self):
        experience = MemoryHit(text="experienced", score=0.5, fact_type="experience")
        world = MemoryHit(text="world fact", score=0.5, fact_type="world")

        curated = curate_recall_hits([world, experience], original_score_weight=0.0)
        # Experience should score higher on reliability
        assert curated[0].text == "experienced"

    def test_source_provenance_boost(self):
        with_source = MemoryHit(text="sourced", score=0.5, source="api:retain")
        no_source = MemoryHit(text="unsourced", score=0.5)

        curated = curate_recall_hits(
            [no_source, with_source], original_score_weight=0.0, freshness_weight=0.0, salience_weight=0.0
        )
        assert curated[0].text == "sourced"


# ---------------------------------------------------------------------------
# 2.4-2.5 Progressive Retrieval + Cross-Source Fusion (type field tests)
# ---------------------------------------------------------------------------


class TestProgressiveRetrievalTypes:
    def test_detail_level_on_recall_request(self):
        r = RecallRequest(query="test", bank_id="b1", detail_level="titles")
        assert r.detail_level == "titles"

    def test_detail_level_default_none(self):
        r = RecallRequest(query="test", bank_id="b1")
        assert r.detail_level is None

    def test_external_context_on_recall_request(self):
        ext = [MemoryHit(text="external RAG result", score=0.8)]
        r = RecallRequest(query="test", bank_id="b1", external_context=ext)
        assert len(r.external_context) == 1
        assert r.external_context[0].text == "external RAG result"

    def test_external_context_default_none(self):
        r = RecallRequest(query="test", bank_id="b1")
        assert r.external_context is None


# ---------------------------------------------------------------------------
# 2.6 Cross-Engine Routing
# ---------------------------------------------------------------------------


class TestAdaptiveRouter:
    def test_temporal_query_boosts_engine(self):
        router = AdaptiveRouter()
        caps = EngineCapabilities(supports_temporal_search=True)
        engine_w, pipeline_w = router.route("What happened last week?", caps)
        assert engine_w > 1.0  # Boosted

    def test_entity_rich_boosts_engine(self):
        router = AdaptiveRouter()
        caps = EngineCapabilities(supports_graph_search=True)
        engine_w, pipeline_w = router.route("What does Calvin at AstrocyteAI prefer?", caps)
        assert engine_w > 1.0

    def test_simple_factual_boosts_pipeline(self):
        router = AdaptiveRouter()
        engine_w, pipeline_w = router.route("What is dark mode?")
        assert pipeline_w >= 1.0

    def test_complex_question_boosts_engine(self):
        router = AdaptiveRouter()
        caps = EngineCapabilities(supports_reflect=True)
        engine_w, pipeline_w = router.route("How does the deployment pipeline handle failures?", caps)
        assert engine_w > 1.0

    def test_short_query_boosts_pipeline(self):
        router = AdaptiveRouter()
        engine_w, pipeline_w = router.route("dark mode")
        assert pipeline_w > 1.0

    def test_no_caps_returns_base_weights(self):
        router = AdaptiveRouter()
        engine_w, pipeline_w = router.route("test query", None, 2.0, 1.5)
        # Without caps, should stay close to base weights
        assert engine_w >= 1.0
        assert pipeline_w >= 1.0

    def test_hybrid_with_adaptive_routing(self):
        from astrocyte.hybrid import HybridEngineProvider

        engine = InMemoryEngineProvider()
        hybrid = HybridEngineProvider(engine=engine, adaptive_routing=True)
        assert hybrid._router is not None

    def test_hybrid_without_adaptive_routing(self):
        from astrocyte.hybrid import HybridEngineProvider

        engine = InMemoryEngineProvider()
        hybrid = HybridEngineProvider(engine=engine, adaptive_routing=False)
        assert hybrid._router is None


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestPhase2Config:
    def test_config_defaults(self):
        config = AstrocyteConfig()
        assert config.recall_cache.enabled is False
        assert config.tiered_retrieval.enabled is False
        assert config.curated_retain.enabled is False
        assert config.curated_recall.enabled is False

    def test_config_values(self):
        config = AstrocyteConfig()
        config.recall_cache.enabled = True
        config.recall_cache.max_entries = 512
        config.tiered_retrieval.enabled = True
        config.tiered_retrieval.max_tier = 4
        config.curated_retain.enabled = True
        config.curated_recall.enabled = True
        config.curated_recall.min_score = 0.3

        assert config.recall_cache.max_entries == 512
        assert config.tiered_retrieval.max_tier == 4
        assert config.curated_recall.min_score == 0.3
