"""B-1 recommendation-shape detector tests.

Validates :func:`is_recommendation_shape` used by
``AstrocyteClient.search`` to decide whether to pin preference-kind
MentalModels at the top of the candidate list. The combined rule must
fire on LME ``single-session-preference`` questions and stay quiet for
temporal / counting / yes-no shapes that don't benefit from preference
anchoring.
"""
from __future__ import annotations

import pytest

from scripts.mem0_harness._shape import is_recommendation_shape


class TestRecommendationShape:
    """Positive cases — must trigger anchor pinning."""

    @pytest.mark.parametrize(
        "question",
        [
            # LME single-session-preference questions that ARE recommendation-
            # shape (Q1, Q2, Q4, Q5 from M14.6 diagnostic — 4 of 5; Q3 is
            # analytical ("do you think it might be...") and intentionally
            # rejected — its experience-kind facts aren't covered by B-1.
            "Can you suggest some accessories that would complement my current photography setup?",
            "Can you suggest some activities that I can do in the evening?",
            "I've been feeling a bit stuck with my paintings lately. Do you have any ideas on how I can find new inspiration?",
            "I'm planning a trip to Denver soon. Any suggestions on what to do there?",
            # Generic recommendation shapes.
            "Recommend a restaurant for my next date",
            "Any tips for my upcoming marathon?",
            "What should I bring on my trip to Tokyo?",
            "Do you have ideas for my anniversary?",
        ],
    )
    def test_positive_recommendation_shapes(self, question: str) -> None:
        assert is_recommendation_shape(question), (
            f"expected recommendation shape: {question!r}"
        )


class TestNonRecommendationShape:
    """Negative cases — must NOT trigger anchor pinning."""

    @pytest.mark.parametrize(
        "question",
        [
            "",  # empty
            # Analytical / diagnostic — not asking for a suggestion.
            "I've been sneezing quite a bit lately. Do you think it might be my living room?",
            # Temporal / counting / yes-no (no recommendation verb).
            "When did I visit Dr. Patel?",
            "How many marathons have I run?",
            "Did I attend the conference last year?",
            "Where did the family vacation take place?",
            # Third-person / general factual (no first-person pronoun).
            "Recommend a restaurant for the guests",
            "Suggest a good algorithm for sorting",
        ],
    )
    def test_negative_non_recommendation_shapes(self, question: str) -> None:
        assert not is_recommendation_shape(question), (
            f"unexpectedly matched recommendation shape: {question!r}"
        )
