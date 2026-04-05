"""LLM-curated retain — the reasoning LLM decides what/how to store.

Inspired by ByteRover: instead of mechanical chunk+embed, the LLM analyzes
incoming content against existing memories and decides ADD/UPDATE/MERGE/SKIP/DELETE.

Async (requires LLM calls).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from astrocyte.types import MemoryHit, Message

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider


@dataclass
class CurationDecision:
    """Result of LLM curation analysis."""

    action: str  # "add" | "update" | "merge" | "skip" | "delete"
    content: str  # Processed content (may be rewritten by LLM)
    memory_layer: str  # "fact" | "observation" | "model"
    reasoning: str  # LLM's explanation for the decision
    merge_target_id: str | None = None  # Memory ID to merge with (for "merge" and "update")


_CURATION_PROMPT = """You are a memory curation agent. Analyze the new content against existing memories and decide the best action.

## Existing memories in this bank (most relevant):
{existing_memories}

## New content to evaluate:
{new_content}

## Actions available:
- ADD: Store as a new memory (genuinely new information)
- UPDATE: Replace an existing memory with updated information (specify which memory_id)
- MERGE: Combine with an existing memory into a richer entry (specify which memory_id)
- SKIP: Don't store (redundant, low-value, or noise)
- DELETE: The new content contradicts/supersedes old info — delete the old memory (specify which memory_id)

## Memory layers:
- fact: Raw factual information
- observation: A pattern or insight derived from multiple facts
- model: A consolidated understanding or mental model

Respond with a JSON object:
{{"action": "add|update|merge|skip|delete", "content": "processed content to store", "memory_layer": "fact|observation|model", "reasoning": "why this action", "merge_target_id": "memory_id or null"}}
"""


async def curate_retain(
    new_content: str,
    existing_memories: list[MemoryHit],
    llm_provider: LLMProvider,
    *,
    model: str | None = None,
) -> CurationDecision:
    """Ask the LLM to curate a retain operation.

    Analyzes new content against existing similar memories and decides
    the best action (ADD/UPDATE/MERGE/SKIP/DELETE) + memory layer classification.

    Returns CurationDecision. Falls back to ADD with memory_layer="fact" on failure.
    """
    # Format existing memories for the prompt
    if existing_memories:
        existing_text = "\n".join(
            f"- [{m.memory_id or 'unknown'}] (score={m.score:.2f}): {m.text}" for m in existing_memories[:5]
        )
    else:
        existing_text = "(no existing memories in this bank)"

    prompt = _CURATION_PROMPT.format(
        existing_memories=existing_text,
        new_content=new_content,
    )

    try:
        completion = await llm_provider.complete(
            messages=[Message(role="user", content=prompt)],
            model=model,
            max_tokens=500,
            temperature=0.0,
        )
        return _parse_curation_response(completion.text, new_content)
    except Exception:
        # Fallback: treat as a simple ADD
        return CurationDecision(
            action="add",
            content=new_content,
            memory_layer="fact",
            reasoning="LLM curation failed, defaulting to ADD",
        )


def _parse_curation_response(response: str, original_content: str) -> CurationDecision:
    """Parse the LLM's curation response JSON."""
    try:
        text = response.strip()
        # Handle markdown code blocks
        if "```" in text:
            start = text.index("```") + 3
            if text[start:].startswith("json"):
                start += 4
            end = text.index("```", start)
            text = text[start:end].strip()

        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")

        action = data.get("action", "add").lower()
        if action not in ("add", "update", "merge", "skip", "delete"):
            action = "add"

        memory_layer = data.get("memory_layer", "fact").lower()
        if memory_layer not in ("fact", "observation", "model"):
            memory_layer = "fact"

        return CurationDecision(
            action=action,
            content=data.get("content", original_content),
            memory_layer=memory_layer,
            reasoning=data.get("reasoning", ""),
            merge_target_id=data.get("merge_target_id"),
        )
    except (json.JSONDecodeError, ValueError):
        return CurationDecision(
            action="add",
            content=original_content,
            memory_layer="fact",
            reasoning="Failed to parse LLM response, defaulting to ADD",
        )
