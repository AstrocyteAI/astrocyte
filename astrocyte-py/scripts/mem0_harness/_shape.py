"""B-1 recommendation-shape detection for preference-anchor pinning.

Standalone module (no heavy deps) so unit tests don't have to import
``astrocyte_client.py`` — which transitively pulls ``pageindex`` and
``litellm``. Keeping this regex helper in its own file keeps the shape
detector test cheap and independently runnable.

The detector is consumed by :meth:`AstrocyteClient.search` to decide
whether to prepend consolidated preference-kind MentalModels at the top
of the candidate list. See the diagnostic in chat notes 2026-05-13
(M14.6 LME single-session-preference analysis) for motivation.
"""
from __future__ import annotations

import re

# Recommendation verbs that signal the user wants a suggestion shaped by
# their personal context (preferences, tools owned, past experience).
_REC_SHAPE_RE = re.compile(
    r"\b(suggest|suggestions?|recommend|recommendations?|ideas?|tips?|"
    r"complement|complements?|any (good|tips)|"
    r"what (should|would|do you (recommend|suggest)|to do))\b",
    re.IGNORECASE,
)

# First-person pronouns — the suggestion must be FOR THE USER, not
# third-party / abstract. Filters out e.g. "recommend a sorting algorithm".
_FIRST_PERSON_RE = re.compile(
    r"(\bmy\b|\bme\b|\bi['’]?(ve|m|d|ll)\b|\bi\b)",
    re.IGNORECASE,
)


def is_recommendation_shape(question: str) -> bool:
    """Return True when ``question`` looks like 'suggest/recommend X for me'.

    Both signals must match: a recommendation verb AND a first-person
    pronoun referring to the user. Either alone is too loose:

    - Verb alone matches "recommend a good Python sorting algorithm"
      (general factual, no personalization needed).
    - Pronoun alone matches "when did I visit Dr. Patel?" (temporal,
      not preference-driven).
    """
    if not question:
        return False
    return bool(_REC_SHAPE_RE.search(question)) and bool(
        _FIRST_PERSON_RE.search(question),
    )
