"""Tests for MIP rule engine — match DSL evaluator."""

import pytest

from astrocyte.mip.rule_engine import (
    RuleEngineInput,
    evaluate_match_block,
    evaluate_match_spec,
    evaluate_rules,
    interpolate_template,
    resolve_field,
)
from astrocyte.mip.schema import ActionSpec, MatchBlock, MatchSpec, RoutingRule


@pytest.fixture
def input_data() -> RuleEngineInput:
    return RuleEngineInput(
        content="Calvin prefers dark mode",
        content_type="student_answer",
        metadata={"student_id": "stu-123", "topic": "algebra", "difficulty": "hard", "attempt_number": 3},
        tags=["preference"],
        pii_detected=False,
        source="tutor-agent",
        signals={"word_count": 50.0, "novelty_score": 0.8},
    )


class TestResolveField:
    def test_top_level_field(self, input_data: RuleEngineInput) -> None:
        assert resolve_field("content_type", input_data) == "student_answer"
        assert resolve_field("pii_detected", input_data) is False
        assert resolve_field("source", input_data) == "tutor-agent"

    def test_metadata_dotted_path(self, input_data: RuleEngineInput) -> None:
        assert resolve_field("metadata.student_id", input_data) == "stu-123"
        assert resolve_field("metadata.topic", input_data) == "algebra"

    def test_signals_dotted_path(self, input_data: RuleEngineInput) -> None:
        assert resolve_field("signals.word_count", input_data) == 50.0
        assert resolve_field("signals.novelty_score", input_data) == 0.8

    def test_missing_field(self, input_data: RuleEngineInput) -> None:
        assert resolve_field("metadata.nonexistent", input_data) is None
        assert resolve_field("unknown_field", input_data) is None


class TestEvaluateMatchSpec:
    def test_exact_match(self, input_data: RuleEngineInput) -> None:
        spec = MatchSpec(field="content_type", operator="eq", value="student_answer")
        assert evaluate_match_spec(spec, input_data) is True

    def test_exact_match_fails(self, input_data: RuleEngineInput) -> None:
        spec = MatchSpec(field="content_type", operator="eq", value="pipeline_event")
        assert evaluate_match_spec(spec, input_data) is False

    def test_in_operator(self, input_data: RuleEngineInput) -> None:
        spec = MatchSpec(field="content_type", operator="in", value=["student_answer", "quiz"])
        assert evaluate_match_spec(spec, input_data) is True

    def test_in_operator_fails(self, input_data: RuleEngineInput) -> None:
        spec = MatchSpec(field="content_type", operator="in", value=["pipeline", "alert"])
        assert evaluate_match_spec(spec, input_data) is False

    def test_gte_operator(self, input_data: RuleEngineInput) -> None:
        spec = MatchSpec(field="signals.word_count", operator="gte", value=50)
        assert evaluate_match_spec(spec, input_data) is True

    def test_lte_operator(self, input_data: RuleEngineInput) -> None:
        spec = MatchSpec(field="signals.word_count", operator="lte", value=100)
        assert evaluate_match_spec(spec, input_data) is True

    def test_gt_lt_operators(self, input_data: RuleEngineInput) -> None:
        assert evaluate_match_spec(MatchSpec(field="signals.word_count", operator="gt", value=49), input_data) is True
        assert evaluate_match_spec(MatchSpec(field="signals.word_count", operator="lt", value=51), input_data) is True
        assert evaluate_match_spec(MatchSpec(field="signals.word_count", operator="gt", value=50), input_data) is False

    def test_present_operator(self, input_data: RuleEngineInput) -> None:
        spec = MatchSpec(field="metadata.student_id", operator="present")
        assert evaluate_match_spec(spec, input_data) is True

    def test_absent_operator(self, input_data: RuleEngineInput) -> None:
        spec = MatchSpec(field="metadata.nonexistent", operator="absent")
        assert evaluate_match_spec(spec, input_data) is True

    def test_present_when_field_missing(self, input_data: RuleEngineInput) -> None:
        spec = MatchSpec(field="metadata.nonexistent", operator="present")
        assert evaluate_match_spec(spec, input_data) is False


class TestEvaluateMatchBlock:
    def test_all_conditions_match(self, input_data: RuleEngineInput) -> None:
        block = MatchBlock(
            all_conditions=[
                MatchSpec(field="content_type", operator="eq", value="student_answer"),
                MatchSpec(field="metadata.student_id", operator="present"),
            ]
        )
        assert evaluate_match_block(block, input_data) is True

    def test_all_conditions_one_fails(self, input_data: RuleEngineInput) -> None:
        block = MatchBlock(
            all_conditions=[
                MatchSpec(field="content_type", operator="eq", value="student_answer"),
                MatchSpec(field="pii_detected", operator="eq", value=True),
            ]
        )
        assert evaluate_match_block(block, input_data) is False

    def test_any_conditions_one_matches(self, input_data: RuleEngineInput) -> None:
        block = MatchBlock(
            any_conditions=[
                MatchSpec(field="content_type", operator="eq", value="wrong"),
                MatchSpec(field="metadata.student_id", operator="present"),
            ]
        )
        assert evaluate_match_block(block, input_data) is True

    def test_any_conditions_none_match(self, input_data: RuleEngineInput) -> None:
        block = MatchBlock(
            any_conditions=[
                MatchSpec(field="content_type", operator="eq", value="wrong1"),
                MatchSpec(field="content_type", operator="eq", value="wrong2"),
            ]
        )
        assert evaluate_match_block(block, input_data) is False

    def test_none_conditions(self, input_data: RuleEngineInput) -> None:
        block = MatchBlock(
            none_conditions=[
                MatchSpec(field="pii_detected", operator="eq", value=True),
            ]
        )
        assert evaluate_match_block(block, input_data) is True

    def test_none_conditions_one_matches(self, input_data: RuleEngineInput) -> None:
        block = MatchBlock(
            none_conditions=[
                MatchSpec(field="content_type", operator="eq", value="student_answer"),
            ]
        )
        assert evaluate_match_block(block, input_data) is False

    def test_empty_all_matches_everything(self, input_data: RuleEngineInput) -> None:
        """Empty all_conditions list = fallback rule."""
        block = MatchBlock(all_conditions=[])
        assert evaluate_match_block(block, input_data) is True


class TestEvaluateRules:
    def test_override_rule_wins(self, input_data: RuleEngineInput) -> None:
        rules = [
            RoutingRule(
                name="normal",
                priority=10,
                match=MatchBlock(
                    all_conditions=[MatchSpec(field="content_type", operator="eq", value="student_answer")]
                ),
                action=ActionSpec(bank="normal-bank"),
            ),
            RoutingRule(
                name="pii-lockdown",
                priority=1,
                override=True,
                match=MatchBlock(all_conditions=[MatchSpec(field="pii_detected", operator="eq", value=True)]),
                action=ActionSpec(bank="private-encrypted"),
            ),
        ]
        # pii_detected is False, override doesn't match
        matches = evaluate_rules(rules, input_data)
        assert len(matches) == 1
        assert matches[0].rule.name == "normal"

    def test_override_rule_short_circuits(self) -> None:
        input_pii = RuleEngineInput(content="has PII", pii_detected=True)
        rules = [
            RoutingRule(
                name="pii-lockdown",
                priority=1,
                override=True,
                match=MatchBlock(all_conditions=[MatchSpec(field="pii_detected", operator="eq", value=True)]),
                action=ActionSpec(bank="private-encrypted"),
            ),
            RoutingRule(
                name="normal",
                priority=10,
                match=MatchBlock(all_conditions=[]),
                action=ActionSpec(bank="normal-bank"),
            ),
        ]
        matches = evaluate_rules(rules, input_pii)
        assert len(matches) == 1
        assert matches[0].rule.name == "pii-lockdown"

    def test_no_match_returns_empty(self, input_data: RuleEngineInput) -> None:
        rules = [
            RoutingRule(
                name="unrelated",
                priority=10,
                match=MatchBlock(
                    all_conditions=[MatchSpec(field="content_type", operator="eq", value="pipeline_event")]
                ),
                action=ActionSpec(bank="ops"),
            ),
        ]
        matches = evaluate_rules(rules, input_data)
        assert len(matches) == 0

    def test_priority_ordering(self, input_data: RuleEngineInput) -> None:
        rules = [
            RoutingRule(
                name="low-priority",
                priority=100,
                match=MatchBlock(all_conditions=[MatchSpec(field="metadata.student_id", operator="present")]),
                action=ActionSpec(bank="general"),
            ),
            RoutingRule(
                name="high-priority",
                priority=5,
                match=MatchBlock(
                    all_conditions=[MatchSpec(field="content_type", operator="eq", value="student_answer")]
                ),
                action=ActionSpec(bank="student"),
            ),
        ]
        matches = evaluate_rules(rules, input_data)
        assert matches[0].rule.name == "high-priority"


class TestInterpolateTemplate:
    def test_simple_interpolation(self, input_data: RuleEngineInput) -> None:
        result = interpolate_template("student-{metadata.student_id}", input_data)
        assert result == "student-stu-123"

    def test_multiple_placeholders(self, input_data: RuleEngineInput) -> None:
        result = interpolate_template("{metadata.topic}-{metadata.difficulty}", input_data)
        assert result == "algebra-hard"

    def test_missing_field_leaves_placeholder(self, input_data: RuleEngineInput) -> None:
        result = interpolate_template("bank-{metadata.nonexistent}", input_data)
        assert result == "bank-{metadata.nonexistent}"

    def test_no_placeholders(self, input_data: RuleEngineInput) -> None:
        result = interpolate_template("static-bank", input_data)
        assert result == "static-bank"
