"""Entity extraction — calls LLM Provider SPI for NER.

Async (I/O-bound). See docs/_design/built-in-pipeline.md section 2.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

from astrocytes.types import Entity, Message

if TYPE_CHECKING:
    from astrocytes.provider import LLMProvider

_EXTRACTION_PROMPT = """Extract named entities from the following text.
Return a JSON array of objects with keys: "name", "entity_type", "aliases".
entity_type must be one of: PERSON, ORG, LOCATION, PRODUCT, EVENT, CONCEPT, OTHER.
aliases should be an array of alternative names (empty array if none).
If no entities are found, return an empty array [].

Text:
{text}

JSON:"""


async def extract_entities(
    text: str,
    llm_provider: LLMProvider,
    model: str | None = None,
) -> list[Entity]:
    """Extract named entities from text via LLM.

    Returns a list of Entity objects. Returns empty list on failure.
    """
    prompt = _EXTRACTION_PROMPT.format(text=text)
    try:
        completion = await llm_provider.complete(
            messages=[Message(role="user", content=prompt)],
            model=model,
            max_tokens=512,
            temperature=0.0,
        )
        return _parse_entities(completion.text)
    except Exception:
        # Entity extraction failure should not block the retain pipeline
        return []


def _parse_entities(response: str) -> list[Entity]:
    """Parse LLM response into Entity objects."""
    try:
        # Try to find JSON array in the response
        text = response.strip()
        # Handle markdown code blocks
        if "```" in text:
            start = text.index("```") + 3
            if text[start:].startswith("json"):
                start += 4
            end = text.index("```", start)
            text = text[start:end].strip()

        entities_data = json.loads(text)
        if not isinstance(entities_data, list):
            return []

        entities: list[Entity] = []
        for item in entities_data:
            if not isinstance(item, dict) or "name" not in item:
                continue
            entities.append(
                Entity(
                    id=uuid.uuid4().hex[:12],
                    name=item["name"],
                    entity_type=item.get("entity_type", "OTHER"),
                    aliases=item.get("aliases", []),
                )
            )
        return entities
    except (json.JSONDecodeError, ValueError):
        return []
