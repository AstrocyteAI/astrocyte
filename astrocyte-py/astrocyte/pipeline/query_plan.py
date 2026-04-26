"""Lightweight query planning for memory synthesis.

This module deliberately stays deterministic and cheap. It identifies the
question shapes that need broader evidence assembly than a single fact lookup:
aggregate/list questions, temporal questions, inference/open-domain questions,
and adversarial/unanswerable-looking prompts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from astrocyte.pipeline.query_intent import QueryIntent, classify_query_intent
from astrocyte.pipeline.temporal import temporal_guidance_for_query


@dataclass(frozen=True)
class QueryPlan:
    """Derived plan for recall and reflect behavior."""

    intent: QueryIntent
    needs_temporal_reasoning: bool = False
    needs_multi_hop_synthesis: bool = False
    needs_aggregate_answer: bool = False
    needs_inference: bool = False
    may_be_adversarial: bool = False
    prompt_variant: str | None = None
    recall_max_results: int = 30
    reflect_rank_limit: int = 12
    reflect_expand_limit: int = 18
    guidance: str | None = None


_AGGREGATE_RE = re.compile(
    r"\b("
    r"what\s+(activities|events|books|items|types|ways|symbols|instruments|artists|bands)|"
    r"how\s+many\s+times|"
    r"in\s+what\s+ways|"
    r"what\s+has\s+\w+\s+(painted|bought|read|done)"
    r")\b",
    re.IGNORECASE,
)
_INFERENCE_RE = re.compile(
    r"\b(would|likely|probably|considered|interested\s+in|prefer|leaning|pursue)\b",
    re.IGNORECASE,
)
_ADVERSARIAL_RE = re.compile(
    r"\b(type\s+of|what\s+type|did\s+\w+\s+make|who\s+is\s+\w+\s+a\s+fan\s+of|what\s+happened\s+to)\b",
    re.IGNORECASE,
)
_MULTI_HOP_RE = re.compile(
    r"\b(across|combine|relationship|support|participat(?:e|ed|ing)|activities|events|ways|types|how\s+many)\b",
    re.IGNORECASE,
)


def build_query_plan(query: str) -> QueryPlan:
    """Classify query shape and choose retrieval/synthesis guidance."""

    query_text = query or ""
    intent = classify_query_intent(query_text).intent
    temporal_guidance = temporal_guidance_for_query(query_text)
    needs_temporal = intent == QueryIntent.TEMPORAL or temporal_guidance is not None
    needs_aggregate = bool(_AGGREGATE_RE.search(query_text))
    needs_inference = bool(_INFERENCE_RE.search(query_text))
    may_be_adversarial = bool(_ADVERSARIAL_RE.search(query_text))
    needs_multi_hop = needs_aggregate or bool(_MULTI_HOP_RE.search(query_text))

    prompt_variant: str | None = None
    if needs_temporal:
        prompt_variant = "temporal_aware"
    elif needs_aggregate or needs_multi_hop:
        prompt_variant = "grounded_synthesis"
    elif needs_inference:
        prompt_variant = "evidence_inference"
    elif may_be_adversarial:
        prompt_variant = "evidence_strict"

    recall_max_results = 30
    reflect_rank_limit = 12
    reflect_expand_limit = 18
    if needs_aggregate or needs_multi_hop:
        recall_max_results = 40
        reflect_rank_limit = 18
        reflect_expand_limit = 26
    if needs_inference:
        recall_max_results = max(recall_max_results, 36)
        reflect_rank_limit = max(reflect_rank_limit, 16)
        reflect_expand_limit = max(reflect_expand_limit, 24)

    guidance_parts: list[str] = []
    if needs_aggregate:
        guidance_parts.append(
            "Aggregate/list question: scan all provided memories for distinct matching facts; "
            "do not stop after the first plausible memory."
        )
    if needs_multi_hop:
        guidance_parts.append(
            "Multi-hop question: combine directly related facts across memories when they share "
            "the same person, event, object, or timeframe."
        )
    if needs_inference:
        guidance_parts.append(
            "Inference question: answer with calibrated language only when the memories directly support it."
        )
    if may_be_adversarial:
        guidance_parts.append(
            "Adversarial check: verify the named person and premise match the memories before answering."
        )
    if temporal_guidance:
        guidance_parts.append(temporal_guidance)

    return QueryPlan(
        intent=intent,
        needs_temporal_reasoning=needs_temporal,
        needs_multi_hop_synthesis=needs_multi_hop,
        needs_aggregate_answer=needs_aggregate,
        needs_inference=needs_inference,
        may_be_adversarial=may_be_adversarial,
        prompt_variant=prompt_variant,
        recall_max_results=recall_max_results,
        reflect_rank_limit=reflect_rank_limit,
        reflect_expand_limit=reflect_expand_limit,
        guidance="\n".join(guidance_parts) if guidance_parts else None,
    )
