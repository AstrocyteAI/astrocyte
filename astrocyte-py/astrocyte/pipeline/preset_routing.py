"""Budget-aware preset routing for benchmark and gateway policy layers."""

from __future__ import annotations

from dataclasses import dataclass

from astrocyte.pipeline.query_intent import QueryIntent, classify_query_intent
from astrocyte.pipeline.query_plan import build_query_plan


@dataclass(frozen=True)
class PresetRoute:
    preset: str
    budget: str
    reason: str


def route_recall_preset(query: str) -> PresetRoute:
    """Choose the lowest-cost preset likely to answer *query* well.

    This is deterministic and intentionally conservative: simple factual
    lookups stay on ``fast-recall``; temporal, relational, comparative, and
    multi-hop questions use the fuller Hindsight-parity budget; exploratory
    questions use ``quality-max`` because synthesis quality matters most.
    """
    plan = build_query_plan(query)
    intent = classify_query_intent(query).intent

    if intent == QueryIntent.EXPLORATORY:
        return PresetRoute("quality-max", "high", "exploratory synthesis")

    if plan.needs_multi_hop_synthesis or intent in {
        QueryIntent.TEMPORAL,
        QueryIntent.RELATIONAL,
        QueryIntent.COMPARATIVE,
    }:
        return PresetRoute("hindsight-parity", "mid", "multi-hop or temporal recall")

    return PresetRoute("fast-recall", "low", "simple lookup")
