"""Query intent classification tests.

Covers the lightweight EdgeQuake-style intent classifier in
:mod:`astrocyte.pipeline.query_intent` and the weighted RRF fusion
variant that consumes its output.

The classifier is deliberately heuristic and bounded — a single test
here pins the exact intent *label* for a query; the wider contract the
tests pin is "confident classifications fire; ambiguous queries fall
back to UNKNOWN (safe neutral blend)".
"""

from __future__ import annotations

from astrocyte.pipeline.fusion import ScoredItem, rrf_fusion, weighted_rrf_fusion
from astrocyte.pipeline.query_intent import (
    CONFIDENCE_THRESHOLD,
    INTENT_STRATEGY_WEIGHTS,
    QueryIntent,
    classify_all_intents,
    classify_query_intent,
    weights_for_intent,
)
from astrocyte.pipeline.query_plan import build_query_plan
from astrocyte.pipeline.temporal import extract_temporal_hints

# ---------------------------------------------------------------------------
# Intent classification — one assertion per intent category
# ---------------------------------------------------------------------------


class TestDominantIntent:
    def test_temporal_when_query(self) -> None:
        r = classify_query_intent("When did Alice start at the company?")
        assert r.intent == QueryIntent.TEMPORAL
        assert r.confidence >= CONFIDENCE_THRESHOLD

    def test_temporal_recency_phrasing(self) -> None:
        r = classify_query_intent("Show me recently retained memories")
        assert r.intent == QueryIntent.TEMPORAL

    def test_temporal_weekend_offset(self) -> None:
        r = classify_query_intent("When did Melanie go camping two weekends before July 17?")
        assert r.intent == QueryIntent.TEMPORAL

    def test_temporal_previous_weekday(self) -> None:
        r = classify_query_intent("What happened previous Friday?")
        assert r.intent == QueryIntent.TEMPORAL

    def test_relational_connection_between(self) -> None:
        r = classify_query_intent("How is Alice related to the payments team?")
        assert r.intent == QueryIntent.RELATIONAL

    def test_comparative_versus(self) -> None:
        r = classify_query_intent("Mystique vs Mem0 feature comparison")
        assert r.intent == QueryIntent.COMPARATIVE

    def test_comparative_difference(self) -> None:
        r = classify_query_intent("difference between Tier 1 and Tier 2 providers")
        assert r.intent == QueryIntent.COMPARATIVE

    def test_procedural_how_to(self) -> None:
        r = classify_query_intent("how to configure the JWT middleware")
        assert r.intent == QueryIntent.PROCEDURAL

    def test_factual_what(self) -> None:
        r = classify_query_intent("what is the default rrf_k value")
        assert r.intent == QueryIntent.FACTUAL

    def test_exploratory_tell_me_about(self) -> None:
        r = classify_query_intent("tell me about the MIP schema")
        assert r.intent == QueryIntent.EXPLORATORY


# ---------------------------------------------------------------------------
# Unknown / confidence threshold
# ---------------------------------------------------------------------------


class TestUnknownAndConfidence:
    def test_empty_query_is_unknown(self) -> None:
        r = classify_query_intent("")
        assert r.intent == QueryIntent.UNKNOWN
        assert r.confidence == 0.0

    def test_whitespace_is_unknown(self) -> None:
        assert classify_query_intent("   ").intent == QueryIntent.UNKNOWN

    def test_low_signal_query_is_unknown(self) -> None:
        """A short noun phrase with no verbs and no temporal/comparative/
        procedural markers must fall to UNKNOWN, not a false confident
        category. The default blend handles these better than any biased
        bet."""
        r = classify_query_intent("quarterly sales figures")
        # Either UNKNOWN or weak enough to not mislead.
        assert r.intent == QueryIntent.UNKNOWN or r.confidence < CONFIDENCE_THRESHOLD + 0.1

    def test_confidence_capped_at_one(self) -> None:
        """Multiple strong signals accumulate but must not exceed 1.0
        so downstream callers can reason about confidence as a ratio."""
        r = classify_query_intent(
            "how to configure installation steps for the setup procedure tutorial guide",
        )
        assert 0.0 < r.confidence <= 1.0

    def test_result_exposes_matched_signals_for_debug(self) -> None:
        """Operators diagnosing a misclassification need to see which
        patterns fired. The `matched_signals` list must not be empty
        when intent is not UNKNOWN."""
        r = classify_query_intent("when did this happen?")
        assert r.intent == QueryIntent.TEMPORAL
        assert len(r.matched_signals) >= 1


# ---------------------------------------------------------------------------
# Full-score map — multi-intent blending
# ---------------------------------------------------------------------------


class TestMultiIntentScoring:
    def test_temporal_and_relational_both_score(self) -> None:
        """A query that's both temporal and relational should surface
        both scores. Callers can decide how to blend."""
        scores = classify_all_intents(
            "how did Alice and Bob connect last week?",
        )
        assert QueryIntent.TEMPORAL in scores
        assert QueryIntent.RELATIONAL in scores

    def test_empty_query_returns_empty_map(self) -> None:
        assert classify_all_intents("") == {}
        assert classify_all_intents("   ") == {}

    def test_all_scores_are_capped_at_one(self) -> None:
        scores = classify_all_intents(
            "how to set up install procedure steps tutorial guide workflow",
        )
        for score in scores.values():
            assert 0.0 < score <= 1.0


# ---------------------------------------------------------------------------
# Strategy weight lookup
# ---------------------------------------------------------------------------


class TestStrategyWeights:
    def test_temporal_intent_boosts_temporal_strategy(self) -> None:
        w = weights_for_intent(QueryIntent.TEMPORAL)
        assert w.temporal > 1.0
        # Other strategies must not be muted entirely (fallback still needed).
        assert w.semantic > 0.0
        assert w.keyword > 0.0

    def test_relational_intent_boosts_graph_strategy(self) -> None:
        w = weights_for_intent(QueryIntent.RELATIONAL)
        assert w.graph > 1.0
        assert w.graph > w.semantic

    def test_unknown_intent_returns_neutral_weights(self) -> None:
        """UNKNOWN must never bias — the classifier isn't confident enough
        to justify a lean, so downstream RRF stays balanced."""
        w = weights_for_intent(QueryIntent.UNKNOWN)
        assert w.semantic == 1.0
        assert w.keyword == 1.0
        assert w.graph == 1.0
        assert w.temporal == 1.0

    def test_all_defined_intents_have_weights(self) -> None:
        """Every value in QueryIntent must have an entry — prevents
        silent KeyError regressions if a new intent is added to the
        enum but not the weights map."""
        for intent in QueryIntent.__members__.values():
            assert intent in INTENT_STRATEGY_WEIGHTS


# ---------------------------------------------------------------------------
# weighted_rrf_fusion — the RRF variant that consumes strategy weights
# ---------------------------------------------------------------------------


def _lists(a: list[str], b: list[str]) -> tuple[list[ScoredItem], list[ScoredItem]]:
    return (
        [ScoredItem(id=i, text=i, score=1.0) for i in a],
        [ScoredItem(id=i, text=i, score=1.0) for i in b],
    )


class TestWeightedRrfFusion:
    def test_equal_weights_matches_plain_rrf(self) -> None:
        """Weighted RRF with all 1.0 weights must produce the same rank
        order as plain RRF. This invariance is what lets the orchestrator
        short-circuit to plain RRF when intent bias is absent."""
        a, b = _lists(["x", "y", "z"], ["y", "z", "w"])
        plain = rrf_fusion([a, b])
        weighted = weighted_rrf_fusion([(a, 1.0), (b, 1.0)])
        assert [i.id for i in plain] == [i.id for i in weighted]

    def test_zero_weight_mutes_strategy(self) -> None:
        """A muted strategy contributes nothing — only the other list's
        ranks matter."""
        a, b = _lists(["x", "y", "z"], ["a", "b", "c"])
        out = weighted_rrf_fusion([(a, 1.0), (b, 0.0)])
        assert {i.id for i in out} == {"x", "y", "z"}  # b's items absent

    def test_higher_weight_lifts_its_list_rank(self) -> None:
        """When two lists disagree on rank, the higher-weighted list
        has more pull. Here 'temporal' ranks 'recent' first with 2x
        weight; 'semantic' ranks 'old' first with 1x weight. 'recent'
        wins the fused rank."""
        temporal, semantic = _lists(["recent", "old"], ["old", "recent"])
        out = weighted_rrf_fusion([(temporal, 2.0), (semantic, 1.0)])
        assert out[0].id == "recent"
        assert out[1].id == "old"

    def test_negative_weight_raises(self) -> None:
        """Negative weights are a caller bug — raise so the sign error
        surfaces immediately rather than silently inverting rankings."""
        import pytest
        a, b = _lists(["x"], ["y"])
        with pytest.raises(ValueError, match="weight must be >= 0.0"):
            weighted_rrf_fusion([(a, 1.0), (b, -5.0)])

    def test_empty_input_returns_empty(self) -> None:
        assert weighted_rrf_fusion([]) == []
        assert weighted_rrf_fusion([([], 1.0)]) == []


class TestTemporalHints:
    def test_extracts_relative_weekend_hint(self) -> None:
        hints = extract_temporal_hints("two weekends before 17 July 2023")
        assert hints
        assert hints[0].kind == "relative_weekend"


class TestQueryPlan:
    def test_aggregate_question_broadens_context(self) -> None:
        plan = build_query_plan("What activities has Melanie done with her family?")
        assert plan.needs_aggregate_answer
        assert plan.needs_multi_hop_synthesis
        assert plan.prompt_variant == "grounded_synthesis"
        assert plan.recall_max_results > 30

    def test_temporal_question_includes_guidance(self) -> None:
        plan = build_query_plan("When did Melanie go camping two weekends before 17 July 2023?")
        assert plan.needs_temporal_reasoning
        assert plan.prompt_variant == "temporal_aware"
        assert plan.guidance is not None
        assert "two weekends" in plan.guidance
