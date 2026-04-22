"""Tests for MIP router — full pipeline: mechanical → escalation → intent."""

import pytest

from astrocyte.mip.router import MipRouter
from astrocyte.mip.rule_engine import RuleEngineInput
from astrocyte.mip.schema import (
    ActionSpec,
    BankDefinition,
    IntentPolicy,
    MatchBlock,
    MatchSpec,
    MipConfig,
    RoutingRule,
)


@pytest.fixture
def student_rules() -> list[RoutingRule]:
    return [
        RoutingRule(
            name="pii-lockdown",
            priority=1,
            override=True,
            match=MatchBlock(all_conditions=[MatchSpec(field="pii_detected", operator="eq", value=True)]),
            action=ActionSpec(bank="private-encrypted", tags=["pii"], retain_policy="redact_before_store"),
        ),
        RoutingRule(
            name="student-answer",
            priority=10,
            match=MatchBlock(
                all_conditions=[
                    MatchSpec(field="content_type", operator="eq", value="student_answer"),
                    MatchSpec(field="metadata.student_id", operator="present"),
                ]
            ),
            action=ActionSpec(bank="student-{metadata.student_id}", tags=["{metadata.topic}"]),
        ),
    ]


@pytest.fixture
def config(student_rules: list[RoutingRule]) -> MipConfig:
    return MipConfig(
        version="1.0",
        banks=[
            BankDefinition(id="student-{student_id}", access=["agent:tutor"]),
            BankDefinition(id="private-encrypted", compliance="pdpa"),
        ],
        rules=student_rules,
    )


class TestRouteSyncMechanical:
    def test_confident_match(self, config: MipConfig) -> None:
        router = MipRouter(config)
        input_data = RuleEngineInput(
            content="2x + 3 = 7, x = 2",
            content_type="student_answer",
            metadata={"student_id": "stu-42", "topic": "algebra"},
        )
        decision = router.route_sync(input_data)
        assert decision is not None
        assert decision.bank_id == "student-stu-42"
        assert decision.tags == ["algebra"]
        assert decision.resolved_by == "mechanical"
        assert decision.rule_name == "student-answer"

    def test_override_rule_locks_out(self, config: MipConfig) -> None:
        router = MipRouter(config)
        input_data = RuleEngineInput(
            content="My SSN is 123-45-6789",
            content_type="student_answer",
            metadata={"student_id": "stu-42"},
            pii_detected=True,
        )
        decision = router.route_sync(input_data)
        assert decision is not None
        assert decision.bank_id == "private-encrypted"
        assert decision.tags == ["pii"]
        assert decision.retain_policy == "redact_before_store"

    def test_no_match_returns_none(self, config: MipConfig) -> None:
        router = MipRouter(config)
        input_data = RuleEngineInput(content="Random content", content_type="text")
        decision = router.route_sync(input_data)
        assert decision is None

    def test_escalation_action_returns_none(self) -> None:
        rules = [
            RoutingRule(
                name="fallback",
                priority=999,
                match=MatchBlock(all_conditions=[]),
                action=ActionSpec(escalate="mip"),
            ),
        ]
        router = MipRouter(MipConfig(rules=rules))
        decision = router.route_sync(RuleEngineInput(content="anything"))
        assert decision is None


class TestRouteAsync:
    @pytest.mark.asyncio
    async def test_mechanical_match_no_llm_needed(self, config: MipConfig) -> None:
        router = MipRouter(config)
        input_data = RuleEngineInput(
            content="x = 5",
            content_type="student_answer",
            metadata={"student_id": "stu-1", "topic": "math"},
        )
        decision = await router.route(input_data)
        assert decision.bank_id == "student-stu-1"
        assert decision.resolved_by == "mechanical"

    @pytest.mark.asyncio
    async def test_passthrough_when_no_match_no_llm(self, config: MipConfig) -> None:
        router = MipRouter(config)
        input_data = RuleEngineInput(content="unclassifiable content")
        decision = await router.route(input_data)
        assert decision.resolved_by == "passthrough"

    @pytest.mark.asyncio
    async def test_escalation_to_intent_with_mock_llm(self) -> None:
        from astrocyte.types import Completion, TokenUsage

        class MockLLM:
            async def complete(self, messages, **kwargs):
                return Completion(
                    text='{"bank_id": "inferred-bank", "tags": ["inferred"], "retain_policy": "default", "reasoning": "LLM decided"}',
                    model="mock",
                    usage=TokenUsage(input_tokens=10, output_tokens=20),
                )

        config = MipConfig(
            banks=[BankDefinition(id="inferred-bank")],
            rules=[],
            intent_policy=IntentPolicy(model_context="Route this content."),
        )
        router = MipRouter(config, llm_provider=MockLLM())
        input_data = RuleEngineInput(content="ambiguous content")
        decision = await router.route(input_data)
        assert decision.resolved_by == "intent"
        assert decision.bank_id == "inferred-bank"
        assert decision.tags == ["inferred"]

    @pytest.mark.asyncio
    async def test_intent_fallback_on_llm_failure(self) -> None:
        class FailingLLM:
            async def complete(self, messages, **kwargs):
                raise RuntimeError("LLM down")

        config = MipConfig(
            rules=[],
            intent_policy=IntentPolicy(model_context="Route this."),
        )
        router = MipRouter(config, llm_provider=FailingLLM())
        input_data = RuleEngineInput(content="content")
        decision = await router.route(input_data)
        assert decision.resolved_by == "passthrough"


class TestRejectPolicy:
    def test_reject_policy_applied(self) -> None:
        rules = [
            RoutingRule(
                name="block-binary",
                priority=1,
                match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="binary")]),
                action=ActionSpec(retain_policy="reject"),
            ),
        ]
        router = MipRouter(MipConfig(rules=rules))
        decision = router.route_sync(RuleEngineInput(content="binary data", content_type="binary"))
        assert decision is not None
        assert decision.retain_policy == "reject"


class TestPipelinePropagation:
    """RoutingDecision must carry the rule's PipelineSpec when set (Phase 1, Step 3)."""

    def test_pipeline_absent_decision_pipeline_none(self) -> None:
        rules = [
            RoutingRule(
                name="r",
                priority=1,
                match=MatchBlock(
                    all_conditions=[MatchSpec(field="content_type", operator="eq", value="chat")]
                ),
                action=ActionSpec(bank="b"),
            ),
        ]
        router = MipRouter(MipConfig(rules=rules))
        decision = router.route_sync(RuleEngineInput(content="hi", content_type="chat"))
        assert decision is not None
        assert decision.pipeline is None

    def test_pipeline_propagates_into_decision(self) -> None:
        from astrocyte.mip.schema import ChunkerSpec, DedupSpec, PipelineSpec

        pipeline = PipelineSpec(
            version=1,
            chunker=ChunkerSpec(strategy="dialogue", max_size=800),
            dedup=DedupSpec(threshold=0.92, action="skip_chunk"),
        )
        rules = [
            RoutingRule(
                name="conv",
                priority=1,
                match=MatchBlock(
                    all_conditions=[MatchSpec(field="content_type", operator="eq", value="conversation")]
                ),
                action=ActionSpec(bank="b", pipeline=pipeline),
            ),
        ]
        router = MipRouter(MipConfig(rules=rules))
        decision = router.route_sync(
            RuleEngineInput(content="hi", content_type="conversation")
        )
        assert decision is not None
        assert decision.pipeline is pipeline
        assert decision.pipeline.chunker.strategy == "dialogue"
        assert decision.pipeline.dedup.threshold == 0.92

    def test_pipeline_propagates_through_override_rule(self) -> None:
        from astrocyte.mip.schema import PipelineSpec, ReflectSpec

        pipeline = PipelineSpec(
            version=1,
            reflect=ReflectSpec(prompt="evidence_strict"),
        )
        rules = [
            RoutingRule(
                name="lock",
                priority=1,
                override=True,
                match=MatchBlock(
                    all_conditions=[MatchSpec(field="content_type", operator="eq", value="legal")]
                ),
                action=ActionSpec(bank="locked", pipeline=pipeline),
            ),
        ]
        router = MipRouter(MipConfig(rules=rules))
        decision = router.route_sync(RuleEngineInput(content="x", content_type="legal"))
        assert decision is not None
        assert decision.pipeline is pipeline
        assert decision.pipeline.reflect.prompt == "evidence_strict"


# ---------------------------------------------------------------------------
# Per-bank pipeline resolution (Phase 2, Step 8b)
# ---------------------------------------------------------------------------


class TestResolvePipelineForBank:
    """``MipRouter.resolve_pipeline_for_bank(bank_id)`` → highest-priority rule's PipelineSpec."""

    def _config(self, rules: list[RoutingRule]) -> MipConfig:
        return MipConfig(rules=rules)

    def test_exact_bank_match(self):
        from astrocyte.mip.schema import PipelineSpec, RerankSpec

        pipe = PipelineSpec(version=1, rerank=RerankSpec(keyword_weight=0.3))
        rule = RoutingRule(
            name="ops",
            priority=10,
            match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="event")]),
            action=ActionSpec(bank="ops-monitoring", pipeline=pipe),
        )
        router = MipRouter(self._config([rule]))
        assert router.resolve_pipeline_for_bank("ops-monitoring") is pipe

    def test_template_bank_match(self):
        from astrocyte.mip.schema import PipelineSpec, RerankSpec

        pipe = PipelineSpec(version=2, rerank=RerankSpec(proper_noun_weight=0.5))
        rule = RoutingRule(
            name="student",
            priority=10,
            match=MatchBlock(all_conditions=[MatchSpec(field="metadata.student_id", operator="present")]),
            action=ActionSpec(bank="student-{metadata.student_id}", pipeline=pipe),
        )
        router = MipRouter(self._config([rule]))
        # Concrete bank_id matches the template
        assert router.resolve_pipeline_for_bank("student-42") is pipe
        assert router.resolve_pipeline_for_bank("student-foo-bar") is pipe
        # Unrelated bank does not match
        assert router.resolve_pipeline_for_bank("not-a-student") is None

    def test_returns_none_when_no_rule_targets_bank(self):
        rule = RoutingRule(
            name="r1",
            priority=10,
            match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="text")]),
            action=ActionSpec(bank="b1"),  # no pipeline
        )
        router = MipRouter(self._config([rule]))
        assert router.resolve_pipeline_for_bank("b1") is None
        assert router.resolve_pipeline_for_bank("unknown") is None

    def test_priority_order_winner(self):
        from astrocyte.mip.schema import PipelineSpec, RerankSpec

        winner = PipelineSpec(version=1, rerank=RerankSpec(keyword_weight=0.99))
        loser = PipelineSpec(version=1, rerank=RerankSpec(keyword_weight=0.01))
        rules = [
            RoutingRule(
                name="low-priority",
                priority=100,
                match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="x")]),
                action=ActionSpec(bank="b1", pipeline=loser),
            ),
            RoutingRule(
                name="high-priority",
                priority=1,
                match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="y")]),
                action=ActionSpec(bank="b1", pipeline=winner),
            ),
        ]
        router = MipRouter(self._config(rules))
        assert router.resolve_pipeline_for_bank("b1") is winner

    def test_skips_rules_without_pipeline(self):
        from astrocyte.mip.schema import PipelineSpec, RerankSpec

        pipe = PipelineSpec(version=1, rerank=RerankSpec(keyword_weight=0.5))
        rules = [
            RoutingRule(
                name="no-pipeline",
                priority=1,
                match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="x")]),
                action=ActionSpec(bank="b1"),
            ),
            RoutingRule(
                name="with-pipeline",
                priority=10,
                match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="y")]),
                action=ActionSpec(bank="b1", pipeline=pipe),
            ),
        ]
        router = MipRouter(self._config(rules))
        assert router.resolve_pipeline_for_bank("b1") is pipe
