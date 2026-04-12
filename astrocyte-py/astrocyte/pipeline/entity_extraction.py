"""Entity extraction — calls LLM Provider SPI for NER.

Async (I/O-bound). See docs/_design/built-in-pipeline.md section 2.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING

from astrocyte.types import Entity, Message

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

logger = logging.getLogger("astrocyte.pipeline")

_EXTRACTION_SYSTEM_PROMPT = """Extract named entities from user-provided text.
Return a JSON array of objects with keys: "name", "entity_type", "aliases".
entity_type must be one of: PERSON, ORG, LOCATION, PRODUCT, EVENT, CONCEPT, OTHER.
aliases should be an array of alternative names (empty array if none).
If no entities are found, return an empty array [].
Respond with ONLY the JSON array, no other text."""


async def extract_entities(
    text: str,
    llm_provider: LLMProvider,
    model: str | None = None,
) -> list[Entity]:
    """Extract named entities from text via LLM.

    Returns a list of Entity objects. Returns empty list on failure.
    """
    user_msg = f"<content>\n{text[:2000]}\n</content>"
    try:
        completion = await llm_provider.complete(
            messages=[
                Message(role="system", content=_EXTRACTION_SYSTEM_PROMPT),
                Message(role="user", content=user_msg),
            ],
            model=model,
            max_tokens=512,
            temperature=0.0,
        )
        return _parse_entities(completion.text)
    except Exception:
        logger.warning("Entity extraction failed, returning empty list", exc_info=True)
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
            close = text.find("```", start)
            if close < 0:
                logger.warning("Malformed markdown code block in entity response")
                return []
            text = text[start:close].strip()

        entities_data = json.loads(text)
        # Handle common LLM wrapper formats: {"entities": [...]}, {"results": [...]}
        if isinstance(entities_data, dict):
            for key in ("entities", "results", "items", "data"):
                if key in entities_data and isinstance(entities_data[key], list):
                    logger.info("Entity extraction: unwrapped JSON from %r key", key)
                    entities_data = entities_data[key]
                    break
        if not isinstance(entities_data, list):
            logger.warning("Entity extraction returned non-list JSON: %s", type(entities_data).__name__)
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
        logger.warning("Entity extraction: failed to parse LLM response as JSON", exc_info=True)
        return []
