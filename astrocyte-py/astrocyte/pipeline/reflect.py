"""Fallback reflect — recall + LLM synthesis.

Async (I/O-bound). See docs/_design/built-in-pipeline.md section 4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrocyte.types import Dispositions, MemoryHit, Message, ReflectResult

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider


def _build_system_prompt(dispositions: Dispositions | None) -> str:
    """Build synthesis system prompt with optional disposition modifiers."""
    base = (
        "You are a memory synthesis agent. "
        "You have been given a set of memories relevant to a query. "
        "Synthesize a clear, concise answer based only on the provided memories. "
        "If the memories do not contain enough information, say so honestly.\n\n"
        "Guidelines:\n"
        "- When the query asks about a specific person, prioritize memories that explicitly mention that person by name.\n"
        "- Consider connections between different memories. If one memory mentions a person and another mentions an event involving that person, combine those facts.\n"
        "- Pay attention to dates and temporal ordering when memories include timestamps.\n"
        "- If multiple memories provide different details about the same topic, synthesize them into a coherent answer."
    )
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


def _format_memories(hits: list[MemoryHit]) -> str:
    """Format memory hits as context for the LLM."""
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
        lines.append(f"{prefix}: {hit.text}")
    return "\n".join(lines)


async def synthesize(
    query: str,
    hits: list[MemoryHit],
    llm_provider: LLMProvider,
    dispositions: Dispositions | None = None,
    max_tokens: int = 2048,
    model: str | None = None,
) -> ReflectResult:
    """Synthesize an answer from recall hits using LLM.

    This is the fallback reflect used when the memory provider
    does not support native reflect.
    """
    if not hits:
        return ReflectResult(
            answer="I don't have any relevant memories to answer this question.",
            sources=[],
        )

    system_prompt = _build_system_prompt(dispositions)
    memories_text = _format_memories(hits)
    user_prompt = (
        f"<memories>\n{memories_text}\n</memories>\n\n"
        f"<query>\n{query}\n</query>"
    )

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
    )
