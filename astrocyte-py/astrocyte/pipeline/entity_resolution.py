"""M11: Entity resolution pipeline stage.

``EntityResolver.resolve()`` takes newly-extracted entities from a retain
call and, for each one, looks for existing candidates in the graph store
that might be the same entity under a different surface form.  When a
candidate is found above the similarity threshold, an LLM confirmation
step is called to verify with a verbatim evidence quote.  Confirmed pairs
produce an ``EntityLink(link_type="alias_of", ...)`` written back to the
graph store.

Design goals
------------
- **Opt-in**: resolver is instantiated only when
  ``entity_resolution: enabled: true`` is in config.  A ``None`` resolver
  in the orchestrator means the stage is a no-op.
- **Bounded cost**: at most ``max_candidates_per_entity`` LLM calls per
  newly-extracted entity, controlled by the caller.
- **Testable**: all I/O goes through the ``GraphStore`` and ``LLMProvider``
  SPIs; ``InMemoryGraphStore`` + ``MockLLMProvider`` cover the full path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from astrocyte.types import Entity, EntityLink, Message

if TYPE_CHECKING:
    from astrocyte.provider import GraphStore, LLMProvider

_logger = logging.getLogger("astrocyte.entity_resolution")

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an entity resolution assistant.

You will be given:
- ENTITY A: a named entity extracted from a new piece of text.
- ENTITY B: a candidate entity already in the knowledge graph.
- SOURCE TEXT: the text that contained Entity A.

Your task: decide whether Entity A and Entity B refer to the same real-world
person, place, organisation, or concept.

Respond with ONLY valid JSON (no markdown, no preamble):

{
  "same_entity": true | false,
  "confidence": <float 0.0–1.0>,
  "evidence": "<verbatim quote from SOURCE TEXT that supports your decision>"
}

Rules:
- Set same_entity=true only when you are confident they refer to the same thing.
- confidence should reflect how certain you are (1.0 = certain, 0.5 = plausible).
- evidence must be a direct quote from SOURCE TEXT, not a paraphrase.
- If same_entity=false, set evidence to an empty string.\
"""


def _build_user_message(entity_a: Entity, entity_b: Entity, source_text: str) -> str:
    aliases_a = f" (also known as: {', '.join(entity_a.aliases)})" if entity_a.aliases else ""
    aliases_b = f" (also known as: {', '.join(entity_b.aliases)})" if entity_b.aliases else ""
    return (
        f"ENTITY A: {entity_a.name}{aliases_a}\n"
        f"ENTITY B: {entity_b.name}{aliases_b}\n\n"
        f"SOURCE TEXT:\n{source_text}"
    )


# ---------------------------------------------------------------------------
# EntityResolver
# ---------------------------------------------------------------------------

class EntityResolver:
    """Identifies and persists alias-of links between entities (M11).

    Args:
        similarity_threshold: Minimum name-similarity required for a
            candidate to be sent to the LLM confirmation step.
            In the in-memory store this is a substring-match gate (0 or 1);
            in production adapters it is a cosine/edit-distance threshold.
        confirmation_threshold: Minimum ``confidence`` from the LLM for a
            link to be written to the graph.  Defaults to ``0.75``.
        max_candidates_per_entity: Hard cap on LLM calls per entity.
            Guards against runaway cost when the graph is large.
    """

    def __init__(
        self,
        *,
        similarity_threshold: float = 0.8,
        confirmation_threshold: float = 0.75,
        max_candidates_per_entity: int = 3,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.confirmation_threshold = confirmation_threshold
        self.max_candidates_per_entity = max_candidates_per_entity

    async def resolve(
        self,
        new_entities: list[Entity],
        source_text: str,
        bank_id: str,
        graph_store: GraphStore,
        llm_provider: LLMProvider,
    ) -> list[EntityLink]:
        """Run entity resolution for a batch of newly-extracted entities.

        For each entity in *new_entities*:
        1. Query ``graph_store.find_entity_candidates`` for similar names.
        2. For each candidate (up to ``max_candidates_per_entity``), call
           the LLM to confirm whether they are the same entity.
        3. If confirmed above ``confirmation_threshold``, write an
           ``alias_of`` link via ``graph_store.store_entity_link``.

        Returns:
            All ``EntityLink`` objects that were written to the graph store.
        """
        written: list[EntityLink] = []

        for entity in new_entities:
            try:
                candidates = await graph_store.find_entity_candidates(
                    entity.name,
                    bank_id,
                    threshold=self.similarity_threshold,
                    limit=self.max_candidates_per_entity + 5,  # fetch a few extra, filter below
                )
            except Exception as exc:
                _logger.warning("find_entity_candidates failed for %r: %s", entity.name, exc)
                continue

            # Skip the entity itself if it appears in candidates (already stored)
            candidates = [c for c in candidates if c.id != entity.id]
            candidates = candidates[: self.max_candidates_per_entity]

            for candidate in candidates:
                link = await self._confirm_and_link(
                    entity, candidate, source_text, bank_id, graph_store, llm_provider
                )
                if link is not None:
                    written.append(link)

        return written

    async def _confirm_and_link(
        self,
        entity_a: Entity,
        entity_b: Entity,
        source_text: str,
        bank_id: str,
        graph_store: GraphStore,
        llm_provider: LLMProvider,
    ) -> EntityLink | None:
        """Ask the LLM to confirm whether two entities are aliases, then store the link."""
        import json

        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=_build_user_message(entity_a, entity_b, source_text)),
        ]

        try:
            completion = await llm_provider.complete(messages, max_tokens=256, temperature=0.0)
            raw = (completion.text or "").strip()
        except Exception as exc:
            _logger.warning(
                "LLM confirmation failed for %r <-> %r: %s", entity_a.name, entity_b.name, exc
            )
            return None

        # Strip markdown fences
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            _logger.warning(
                "entity resolution LLM returned non-JSON for %r <-> %r: %r",
                entity_a.name, entity_b.name, raw[:120],
            )
            return None

        same = bool(data.get("same_entity", False))
        if not same:
            return None

        confidence_raw = data.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence_raw)))
        except (TypeError, ValueError):
            confidence = 0.0

        if confidence < self.confirmation_threshold:
            _logger.debug(
                "entity resolution: %r <-> %r confidence %.2f below threshold %.2f — skipped",
                entity_a.name, entity_b.name, confidence, self.confirmation_threshold,
            )
            return None

        evidence = str(data.get("evidence", ""))

        link = EntityLink(
            entity_a=entity_a.id,
            entity_b=entity_b.id,
            link_type="alias_of",
            evidence=evidence,
            confidence=confidence,
            created_at=datetime.now(timezone.utc),
        )

        try:
            await graph_store.store_entity_link(link, bank_id)
        except Exception as exc:
            _logger.warning(
                "store_entity_link failed for %r <-> %r: %s", entity_a.name, entity_b.name, exc
            )
            return None

        _logger.info(
            "entity resolution: linked %r (%s) alias_of %r (%s) confidence=%.2f",
            entity_a.name, entity_a.id, entity_b.name, entity_b.id, confidence,
        )
        return link
