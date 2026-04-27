"""Fallback reflect — recall + LLM synthesis.

Async (I/O-bound). See docs/_design/built-in-pipeline.md section 4.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from astrocyte.mip.schema import ReflectSpec
from astrocyte.pipeline.query_plan import build_query_plan
from astrocyte.types import Dispositions, MemoryHit, Message, ReflectResult

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider


# Prompt registry — referenced by ReflectSpec.prompt. Unknown names fall back to
# "default" so a typo never breaks reflect; the loader/lint step is responsible
# for catching unknown names early. Hard cap on metadata keys promoted into the
# memory render is enforced here (P4 — defense in depth).
_PROMOTE_METADATA_MAX = 5

_DEFAULT_PROMPT = (
    "You are a memory synthesis agent. "
    "You have been given a set of memories relevant to a query. "
    "Synthesize a clear, concise answer based ONLY on what is explicitly stated in the provided memories. "
    "Before saying information is unavailable, inspect every provided memory for directly supporting facts. "
    "Do not draw on outside knowledge or stereotypes. "
    "You may combine directly related memories when they share the same person, event, object, or timeframe, "
    "but do not connect merely tangential memories.\n\n"
    "Guidelines:\n"
    "- When the query asks about a specific person, prioritize memories that explicitly mention that person by name.\n"
    "- Consider connections between different memories. If one memory mentions a person and another mentions an event involving that person, combine those facts.\n"
    "- Pay attention to dates and temporal ordering when memories include timestamps.\n"
    "- If multiple memories provide different details about the same topic, synthesize them into a coherent answer.\n"
    "- If directly supporting memories exist, answer from them rather than saying the information is unavailable.\n"
    "- If no provided memory directly supports the answer, respond with: 'This information is not available in my memories.'\n"
    "- If the question contains a false or unverifiable premise, say so explicitly rather than answering as if the premise were true."
)

_TEMPORAL_AWARE_PROMPT = (
    "You are a memory synthesis agent answering a question about events over time. "
    "Answer ONLY from what is explicitly stated in the provided memories. "
    "If the specific information is not present, respond with: "
    "'This information is not available in my memories.'\n\n"
    "Guidelines:\n"
    "- Treat timestamps as load-bearing: order memories chronologically before answering.\n"
    "- When a question asks about ordering ('before', 'after', 'first', 'last'), justify the answer with the relevant dates.\n"
    "- Distinguish between when an event occurred and when it was recorded.\n"
    "- Normalize relative temporal phrases against the memory timestamp: 'last week', 'previous Friday', "
    "'yesterday', 'two weekends ago', and similar phrases should be resolved from the recorded date.\n"
    "- If the memory says an event happened last week, answer in that relative form when useful "
    "(for example, 'the week before 9 June 2023') instead of saying it happened on the record date.\n"
    "- If timestamps are missing or ambiguous, say so rather than guessing.\n"
    "- Do not infer a timeline from unrelated clues; only compute dates from explicit temporal phrases and memory timestamps."
)

_EVIDENCE_STRICT_PROMPT = (
    "You are a memory synthesis agent operating under strict evidence rules.\n\n"
    "Guidelines:\n"
    "- Answer ONLY from the memories provided. Do not draw on outside knowledge.\n"
    "- Cite the specific memory number ('Memory 3') for every claim.\n"
    "- If the memories do not contain a definitive answer, say 'Insufficient evidence in the provided memories.'\n"
    "- Do not paraphrase loosely; preserve nuance, qualifications, and uncertainty markers."
)

_EVIDENCE_INFERENCE_PROMPT = (
    "You are a memory synthesis agent answering an inference question from personal memories. "
    "Use ONLY the provided memories as evidence; do not use outside facts or stereotypes. "
    "Unlike strict fact lookup, you MAY make a cautious inference when the question asks what someone "
    "would likely do, prefer, believe, or be considered, as long as the inference is directly supported "
    "by retrieved memories.\n\n"
    "Guidelines:\n"
    "- For 'would' or 'likely' questions, answer with calibrated language such as 'Likely yes' or 'Likely no' plus the evidence.\n"
    "- Connect preferences, repeated activities, stated goals, and identity facts across memories when they support the inference.\n"
    "- If the memories support multiple possibilities, say which is more likely and why.\n"
    "- If the memories contain no relevant evidence, respond with: 'This information is not available in my memories.'\n"
    "- Never invent facts; every inference must be traceable to the provided memories."
)

_GROUNDED_SYNTHESIS_PROMPT = (
    "You are a memory synthesis agent for aggregate and multi-hop questions. "
    "Use ONLY the provided memories, but actively combine directly related memories when needed.\n\n"
    "Guidelines:\n"
    "- Scan all provided memories before answering; do not stop at the first matching memory.\n"
    "- For list questions, collect distinct answer items and omit unsupported distractors.\n"
    "- For count questions, count only evidence-backed occurrences and explain uncertainty briefly when needed.\n"
    "- For multi-hop questions, connect facts only when they share the same person, event, object, or timestamp context.\n"
    "- If relevant memories are present but incomplete, answer the supported part instead of saying everything is unavailable.\n"
    "- If no provided memory directly supports the answer, respond with: 'This information is not available in my memories.'\n"
    "- If the question contains a false or wrong-person premise, reject that premise rather than answering from a similar memory."
)

PROMPT_REGISTRY: dict[str, str] = {
    "default": _DEFAULT_PROMPT,
    "temporal_aware": _TEMPORAL_AWARE_PROMPT,
    "evidence_strict": _EVIDENCE_STRICT_PROMPT,
    "evidence_inference": _EVIDENCE_INFERENCE_PROMPT,
    "grounded_synthesis": _GROUNDED_SYNTHESIS_PROMPT,
}

_INFERENCE_QUERY_RE = re.compile(
    r"\b(would|likely|probably|considered|interested\s+in|prefer|leaning|pursue)\b",
    re.IGNORECASE,
)


def _auto_prompt_variant(query: str) -> str | None:
    """Select a reflect prompt variant from lightweight query cues.

    Explicit MIP ``ReflectSpec.prompt`` still wins; this helper is only used
    when the bank has no prompt override. Temporal takes precedence because
    date math needs stricter handling than general inference.
    """
    from astrocyte.pipeline.query_intent import QueryIntent, classify_query_intent

    query_plan = build_query_plan(query)
    if query_plan.prompt_variant is not None:
        return query_plan.prompt_variant
    intent = classify_query_intent(query).intent
    if intent == QueryIntent.TEMPORAL:
        return "temporal_aware"
    if _INFERENCE_QUERY_RE.search(query or ""):
        return "evidence_inference"
    return None


def _build_system_prompt(
    dispositions: Dispositions | None,
    *,
    prompt_variant: str | None = None,
) -> str:
    """Build synthesis system prompt with optional disposition modifiers.

    ``prompt_variant`` selects from :data:`PROMPT_REGISTRY` (``"default"``,
    ``"temporal_aware"``, ``"evidence_strict"``). Unknown names fall back to
    ``"default"`` — the lint/loader path is responsible for catching typos.
    """
    base = PROMPT_REGISTRY.get(prompt_variant or "default", _DEFAULT_PROMPT)
    if dispositions:
        traits: list[str] = []
        if dispositions.skepticism >= 4:
            traits.append("Be skeptical of uncertain claims and note where evidence is weak.")
        elif dispositions.skepticism <= 2:
            traits.append("Trust the memories at face value unless clearly contradictory.")
        if dispositions.literalism >= 4:
            traits.append("Interpret memories literally and precisely.")
        elif dispositions.literalism <= 2:
            traits.append("Interpret memories flexibly, considering context and intent.")
        if dispositions.empathy >= 4:
            traits.append("Acknowledge the human experience behind the memories.")
        elif dispositions.empathy <= 2:
            traits.append("Focus on factual content without emotional framing.")
        if traits:
            base += "\n\n" + " ".join(traits)
    return base


def _format_memories(
    hits: list[MemoryHit],
    *,
    promote_metadata: list[str] | None = None,
) -> str:
    """Format memory hits as context for the LLM.

    ``promote_metadata`` lists metadata keys to surface inline alongside each
    memory's prefix (e.g. ``["author", "source_url"]``). The list is hard-capped
    at :data:`_PROMOTE_METADATA_MAX` (P4 — keeps the prompt budget bounded);
    excess keys are silently dropped. Keys missing on a given hit are skipped
    rather than rendered as ``None``.
    """
    promoted: list[str] = list(promote_metadata or [])[:_PROMOTE_METADATA_MAX]
    lines: list[str] = []
    for i, hit in enumerate(hits, 1):
        prefix = f"[Memory {i}]"
        if hit.fact_type:
            prefix += f" ({hit.fact_type})"
        # Prefer occurred_at timestamp; fall back to date_time from metadata
        if hit.occurred_at:
            prefix += f" [{hit.occurred_at.isoformat()}]"
        elif hit.metadata and hit.metadata.get("date_time"):
            prefix += f" [{hit.metadata['date_time']}]"
        if hit.metadata and hit.metadata.get("resolved_date"):
            prefix += (
                f" {{temporal_phrase={hit.metadata.get('temporal_phrase')}, "
                f"resolved_date={hit.metadata.get('resolved_date')}, "
                f"granularity={hit.metadata.get('date_granularity')}}}"
            )
        # Promoted metadata fields appended in declared order
        if promoted and hit.metadata:
            extras = [f"{key}={hit.metadata[key]}" for key in promoted if key in hit.metadata]
            if extras:
                prefix += " {" + ", ".join(extras) + "}"
        lines.append(f"{prefix}: {hit.text}")
    return "\n".join(lines)


async def synthesize(
    query: str,
    hits: list[MemoryHit],
    llm_provider: LLMProvider,
    dispositions: Dispositions | None = None,
    max_tokens: int = 2048,
    model: str | None = None,
    authority_context: str | None = None,
    mip_reflect: ReflectSpec | None = None,
) -> ReflectResult:
    """Synthesize an answer from recall hits using LLM.

    This is the fallback reflect used when the memory provider
    does not support native reflect.

    ``mip_reflect`` (optional) carries the active rule's ReflectSpec — its
    ``prompt`` selects from :data:`PROMPT_REGISTRY` and ``promote_metadata``
    lifts metadata fields into the rendered memory block (capped at 5 by P4).
    """
    if not hits:
        return ReflectResult(
            answer="I don't have any relevant memories to answer this question.",
            sources=[],
            authority_context=authority_context,
        )

    prompt_variant = mip_reflect.prompt if mip_reflect is not None else None
    promote_metadata = mip_reflect.promote_metadata if mip_reflect is not None else None
    system_prompt = _build_system_prompt(dispositions, prompt_variant=prompt_variant)
    memories_text = _format_memories(hits, promote_metadata=promote_metadata)
    query_plan = build_query_plan(query)
    user_prompt = f"<memories>\n{memories_text}\n</memories>\n\n<query>\n{query}\n</query>"
    if query_plan.guidance:
        user_prompt = (
            f"<query_guidance>\n{query_plan.guidance}\n</query_guidance>\n\n"
            + user_prompt
        )
    if authority_context and str(authority_context).strip():
        user_prompt = f"<authority_context>\n{authority_context.strip()}\n</authority_context>\n\n" + user_prompt

    completion = await llm_provider.complete(
        messages=[
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt),
        ],
        model=model,
        max_tokens=max_tokens,
        temperature=0.1,
    )

    return ReflectResult(
        answer=completion.text,
        sources=hits,
        authority_context=authority_context,
    )
