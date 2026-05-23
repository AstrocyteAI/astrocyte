"""Per-question reflect routing (M36 — M21's deferred per-Q-type routing).

When ``ASTROCYTE_USE_REFLECT=1``, route only **temporal** questions to
the agentic reflect loop; everything else stays on the standard
recall + answerer path. Implements the M20 §8.3/§8.5 finding that
reflect helps temporal-reasoning (+1 to +5 questions across R1
variants) but regresses synthesis-heavy categories
(single-session-preference, multi-session) when used as the default.

Routing signal
--------------

Cheap, regex+dateparser-based: if
:func:`astrocyte.pipeline.query_analyzer.analyze_query` returns a
bounded ``temporal_constraint`` on the question, route to reflect.
Otherwise route to the standard pipeline.

We deliberately use the same signal that gates the temporal RRF sibling
in fact_recall — questions that already trigger temporal retrieval are
the same ones that benefit from iterative date-narrowing in reflect.
Reusing the signal keeps the architecture coherent: temporal-aware
recall + temporal-aware answer composition fire together.

Why not the bench's ``qa["category"]``
-------------------------------------

The bench knows each question's ground-truth category. We could route
on that for the oracle measurement. We don't, because:

  1. Production callers don't have a category label — they have a
     question string. Routing on the bench category overestimates
     real-world lift.
  2. The query-analyzer signal is what production code would actually
     use; benching it is the honest test.

References
----------

- ``docs/_design/m20-reflect-agent.md`` §8.3 — per-Q-type routing
  projected +5q LME, deferred to M21.
- ``docs/_design/m36-reflect-loop.md`` — revised plan now that the
  agentic reflect loop already exists.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

_logger = logging.getLogger("astrocyte.mem0_harness.reflect_routing")


# M36 — supplementary regex patterns. The query analyzer detects
# queries with embedded dates ("a week ago" → range). It DOESN'T catch
# the family of temporal-reasoning questions that ASK ABOUT durations
# or orderings — those phrasings have no extractable date, but the
# answer requires iterative fact-finding + date arithmetic, which is
# exactly what reflect's loop is good at.
#
# Examples that don't trigger dateparser but DO need reflect:
#   - "How many weeks ago did I attend the festival?"
#   - "How long ago did I quit smoking?"
#   - "When did I first try sushi?"
#   - "What time do I usually wake up on Tuesdays?"
#   - "Which event came first, X or Y?"
#   - "Did I tell you about X before or after Y?"
#
# Each pattern is a regex compiled once. Routing is OR over all
# patterns plus the query-analyzer signal.
_TEMPORAL_REASONING_PATTERNS: tuple[re.Pattern, ...] = (
    # "how many <weeks|days|months|years|hours|minutes>"
    re.compile(r"\bhow\s+many\s+(weeks?|days?|months?|years?|hours?|minutes?)\b", re.IGNORECASE),
    # "how long" / "how long ago" / "how long since" / "how long until"
    re.compile(r"\bhow\s+long\b", re.IGNORECASE),
    # "when did I/we ..." (first / last / start / begin / quit / stop)
    re.compile(r"\bwhen\s+(?:did|do|does|have)\s+(?:i|we|you)\b", re.IGNORECASE),
    # "what time do I/we ..." (recurring schedule)
    re.compile(r"\bwhat\s+time\s+(?:do|did|does)\s+(?:i|we|you)\b", re.IGNORECASE),
    # "which event/thing/X (came|came in|happened|was) first/last/earliest/latest"
    re.compile(r"\bwhich\s+\w+\s+(?:came|came\s+in|happened|was|did)\s+(first|last|earliest|latest)\b", re.IGNORECASE),
    # "before or after" / "earlier or later"
    re.compile(r"\b(?:before|after)\s+(?:or\s+(?:after|before))\b", re.IGNORECASE),
    # "first time" / "last time" qualifiers
    re.compile(r"\b(?:first|last)\s+time\s+(?:i|we|you)\b", re.IGNORECASE),
)


def _matches_temporal_reasoning_pattern(question: str) -> bool:
    """Cheap regex check for temporal-reasoning questions that lack
    explicit dates. Returns True if any pattern matches."""
    return any(pat.search(question) for pat in _TEMPORAL_REASONING_PATTERNS)


def is_reflect_enabled() -> bool:
    """``ASTROCYTE_USE_REFLECT`` set to a truthy value."""
    return os.environ.get("ASTROCYTE_USE_REFLECT", "").lower() in ("1", "true", "yes")


def is_hybrid_routing() -> bool:
    """``ASTROCYTE_USE_REFLECT`` set to ``hybrid`` / ``auto``.

    When True, ``_reflect_process_question`` consults
    :func:`should_use_reflect_for_question` per-question and routes
    only matching questions through the reflect loop.

    When False (legacy: ``ASTROCYTE_USE_REFLECT=1``), every question
    goes through reflect — preserves M20's all-or-nothing behaviour.

    Default for v0.15.0 (M36): **hybrid is on whenever reflect is
    enabled**. The legacy all-on mode regressed in M20; hybrid is the
    M21-deferred fix that projected +5q.
    """
    raw = os.environ.get("ASTROCYTE_USE_REFLECT", "").lower()
    if raw in ("hybrid", "auto"):
        return True
    # M36 default: when reflect is on, hybrid routing is the new
    # default behaviour. Set ASTROCYTE_USE_REFLECT_HYBRID=0 to revert
    # to all-on (M20 behaviour) for ablation.
    if raw in ("1", "true", "yes"):
        return os.environ.get("ASTROCYTE_USE_REFLECT_HYBRID", "1").lower() in ("1", "true", "yes")
    return False


async def should_use_reflect_for_question(
    question: str,
    *,
    reference_date: datetime | None = None,
) -> bool:
    """True when the question should be routed to the reflect loop.

    M36 routing rule: a question that triggers temporal extraction
    (regex Pass A or dateparser Pass B) is a candidate for reflect's
    iterative date-narrowing. Everything else stays on the standard
    recall+answerer path.

    Args:
        question: Raw question text.
        reference_date: Anchor for relative phrases ("a week ago" →
            anchor - 7d). Typically the question's ``question_date``
            from the LME/LoCoMo dataset. When None, ``datetime.now()``
            is used (preserves test-bench parity).

    Returns:
        True → route to ``mem0.reflect()``. False → standard
        ``recall + answerer`` path.

    Safety
    ------

    Any exception in the analyzer → return False (default to the
    cheaper, well-validated path). A parser bug must not make us
    accidentally route everything through reflect's longer loop.
    """
    # M36 — first check the cheap supplementary regex patterns for
    # temporal-reasoning forms that don't have extractable dates
    # ("how many weeks ago", "when did I first", "which came first",
    # "before or after"). These are exactly the questions that
    # benefit most from reflect's iterative date-narrowing.
    if _matches_temporal_reasoning_pattern(question):
        return True

    try:
        from astrocyte.pipeline.query_analyzer import analyze_query  # noqa: PLC0415

        analysis = await analyze_query(
            question,
            reference_date=reference_date,
            llm_provider=None,
            allow_llm_fallback=False,
            allow_temporal_expansion=True,
        )
        tc = analysis.temporal_constraint
        return bool(tc and tc.is_bounded())
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "reflect_routing: analyze_query raised %s on question; defaulting to non-reflect path: %s",
            type(exc).__name__,
            exc,
        )
        return False


__all__ = [
    "is_reflect_enabled",
    "is_hybrid_routing",
    "should_use_reflect_for_question",
]
