"""Tests for M34-1 — intent → channel weights mapping."""

from __future__ import annotations

import pytest

from astrocyte.pipeline.intent_weights import (
    INTENT_CHANNEL_WEIGHTS,
    NEUTRAL_WEIGHTS,
    ChannelWeights,
    weights_for_intent,
)
from astrocyte.pipeline.query_intent import QueryIntent


class TestChannelWeightsDefaults:
    def test_default_weights_all_neutral(self) -> None:
        w = ChannelWeights()
        assert w.semantic == 1.0
        assert w.episodic == 1.0
        assert w.temporal == 1.0
        assert w.link_expansion == 1.0
        assert w.bm25 == 1.0

    def test_frozen(self) -> None:
        # Dataclass(frozen=True) — must reject attribute mutation.
        w = ChannelWeights()
        with pytest.raises((AttributeError, TypeError)):
            w.semantic = 0.5  # type: ignore[misc]


class TestPerIntentWeights:
    """Pin the M34 calibration table. Each intent's biased channel must
    point in the intended direction (boost or damp) so future edits stay
    deliberate; absolute values can shift without breaking these tests
    as long as the direction is preserved."""

    def test_temporal_intent_boosts_temporal_channel(self) -> None:
        # TR queries depend on temporal recall — that channel must be
        # the highest-weighted among the temporal-intent weights.
        w = INTENT_CHANNEL_WEIGHTS[QueryIntent.TEMPORAL]
        assert w.temporal > w.semantic
        assert w.temporal > w.link_expansion
        assert w.temporal > w.episodic
        assert w.temporal >= 1.5  # explicit boost contract

    def test_factual_intent_boosts_semantic_and_bm25(self) -> None:
        # SSU-style "what is my X" lookups depend on semantic + BM25.
        w = INTENT_CHANNEL_WEIGHTS[QueryIntent.FACTUAL]
        assert w.semantic >= 1.5
        assert w.bm25 >= 1.5
        # ... and damps temporal so date-window noise doesn't pollute.
        assert w.temporal < 0.5

    def test_relational_intent_boosts_link_expansion(self) -> None:
        # MS-style "across our chats" queries depend on cross-session
        # link expansion.
        w = INTENT_CHANNEL_WEIGHTS[QueryIntent.RELATIONAL]
        assert w.link_expansion >= 1.5
        assert w.link_expansion > w.temporal
        assert w.link_expansion > w.semantic

    def test_comparative_intent_damps_temporal(self) -> None:
        # "Which X first" — dateparser fires false-positives on weekday
        # names and event-mention dates. Damp temporal to <0.5.
        w = INTENT_CHANNEL_WEIGHTS[QueryIntent.COMPARATIVE]
        assert w.temporal < 0.5

    def test_procedural_intent_semantic_led(self) -> None:
        # "How to" queries are semantic-led with BM25 as fallback for
        # specific tool names.
        w = INTENT_CHANNEL_WEIGHTS[QueryIntent.PROCEDURAL]
        assert w.semantic > 1.0
        assert w.temporal < 0.5

    def test_exploratory_intent_neutral_blend(self) -> None:
        # "Tell me about X" — diversity matters more than precision.
        # All channels stay at 1.0.
        w = INTENT_CHANNEL_WEIGHTS[QueryIntent.EXPLORATORY]
        assert w.semantic == 1.0
        assert w.temporal == 1.0
        assert w.link_expansion == 1.0
        assert w.bm25 == 1.0

    def test_unknown_intent_safe_fallback(self) -> None:
        # UNKNOWN must never produce a more aggressive profile than
        # the worst real intent — it's the safety net when the
        # classifier punts. All weights should be ≤ 1.0 (no boosts).
        w = INTENT_CHANNEL_WEIGHTS[QueryIntent.UNKNOWN]
        assert w.semantic <= 1.0
        assert w.episodic <= 1.0
        assert w.temporal <= 1.0
        assert w.link_expansion <= 1.0
        assert w.bm25 <= 1.0

    def test_no_intent_uses_zero_weight(self) -> None:
        # Hard mutes invite silent failure when the classifier misfires;
        # every channel must keep at least token participation. Future
        # additions should preserve this invariant.
        for intent, w in INTENT_CHANNEL_WEIGHTS.items():
            for name in ("semantic", "episodic", "temporal", "link_expansion", "bm25"):
                val = getattr(w, name)
                assert val > 0.0, (
                    f"{intent}: channel {name} has zero weight — use a small "
                    "positive value instead to keep graceful degradation."
                )

    def test_no_intent_exceeds_max_boost(self) -> None:
        # Keep RRF stable: cap boost at 1.5. A 2.0-weighted channel
        # would overwhelm the others; that's a knob to add deliberately
        # in a future cycle, not silently.
        for intent, w in INTENT_CHANNEL_WEIGHTS.items():
            for name in ("semantic", "episodic", "temporal", "link_expansion", "bm25"):
                val = getattr(w, name)
                assert val <= 1.5, (
                    f"{intent}: channel {name}={val} exceeds 1.5 boost cap"
                )


class TestWeightsForIntent:
    def test_none_returns_neutral_baseline(self) -> None:
        assert weights_for_intent(None) is NEUTRAL_WEIGHTS

    def test_known_intent_returns_table_entry(self) -> None:
        w = weights_for_intent(QueryIntent.TEMPORAL)
        assert w is INTENT_CHANNEL_WEIGHTS[QueryIntent.TEMPORAL]

    def test_unknown_intent_explicit_returns_unknown_entry(self) -> None:
        w = weights_for_intent(QueryIntent.UNKNOWN)
        assert w is INTENT_CHANNEL_WEIGHTS[QueryIntent.UNKNOWN]
        # ... which equals the neutral baseline.
        assert w is NEUTRAL_WEIGHTS

    def test_all_query_intents_have_a_table_entry(self) -> None:
        # If a new QueryIntent variant is added but no weight entry is
        # defined, the lookup falls back to NEUTRAL_WEIGHTS — silent
        # behaviour. This test fails loudly so a developer must update
        # the table deliberately.
        for intent in QueryIntent:
            assert intent in INTENT_CHANNEL_WEIGHTS, (
                f"QueryIntent.{intent.name} has no INTENT_CHANNEL_WEIGHTS "
                f"entry; falling back to NEUTRAL silently. Add an "
                f"explicit entry."
            )
