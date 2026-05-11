"""M12.4: entity-graph expansion over fact-grain hits — REVERTED EXPERIMENT.

**Status:** Implemented but not wired into the bench pipeline. The bench
gate at M12.4 showed LME regressed -4.5pp (55.5→51.0) with
multi-session dropping 11.8→2.9 — the exact category expansion was
supposed to help. LoCoMo was flat (small gains on multi-hop +2.5,
temporal +2.5 offset by losses on adversarial -2.4, open-domain -2.7).

Root cause hypothesis: LME's user-haystack has 50+ sessions with dense
entity overlap (same user, recurring topics). Naive co-occurrence
expansion floods the candidate pool with off-topic but entity-linked
facts that the cross-encoder rerank can't filter on a per-fact basis.
LoCoMo's 10-conversation graph is sparse enough that signal/noise
breaks even.

Kept for: documentation of what was tried, the test suite as a
contract pin if a future attempt re-uses this primitive with a
smarter gating strategy (e.g. only expand when entities are RARE
across the bank, or only when the picker selected ≥2 lines).

Sits between fact semantic-retrieval and fact rerank. For multi-hop
and multi-session questions, the question's anchor entities and the
answer's anchor entities are different — the bridge is a chain of
co-occurring entities across sections. The bi-encoder semantic search
finds facts that are textually similar to the question, but it can't
follow that chain.

This module follows it. Given the top-K semantic hits, collect their
entities, find OTHER facts that mention those entities (cross-section),
and return the expanded set. The downstream cross-encoder rerank
([fact_rerank.py]) picks which expanded facts actually answer the
question.

Generic across benches — entity strings (proper nouns + typed labels
like ``role:doctor``) are bench-agnostic. No question parsing, no LLM
call: the seed entities come from the bi-encoder's own top hits.

Design knob trade-offs:

- ``max_seed_entities``: too high and we expand from noisy entities
  the bi-encoder happened to surface; too low and we miss valid
  bridges. 8 is the Hindsight default for similar graph-walk depth.
- ``max_neighbor_facts_per_entity``: caps the fan-out per seed entity.
  A common entity like "User" could pull thousands of facts; the cap
  keeps total candidates bounded.
- ``max_expanded_facts``: total cap across all entities. Combined with
  the downstream rerank's ``rerank_top_k=30``, this bounds inference
  cost.

See:
- ``docs/_design/recall.md`` §15 (M12.4)
- ``astrocyte.pipeline.fact_rerank`` for the next stage
- Hindsight's ``search_unit_links`` for the section-grain analogue
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.provider import PageIndexStore
    from astrocyte.types import PageIndexFactHit

logger = logging.getLogger("astrocyte.pipeline.fact_entity_expansion")


async def expand_via_entity_graph(
    initial_hits: list[PageIndexFactHit],
    *,
    store: PageIndexStore,
    bank_id: str,
    document_id: str | None = None,
    max_seed_hits: int = 5,
    max_seed_entities: int = 8,
    max_neighbor_facts_per_entity: int = 10,
    max_expanded_facts: int = 20,
) -> list[PageIndexFactHit]:
    """Expand a candidate fact set by walking entity co-occurrence.

    Args:
        initial_hits: Top-ranked semantic / picker-filtered fact hits.
            Their entities seed the expansion.
        store: PageIndexStore for entity-anchored fact lookup.
        bank_id: Bank scope for all lookups.
        document_id: Optional doc-scope — if set, only facts within
            this document are considered. ``None`` lets the expansion
            cross documents (useful for LME multi-session, where the
            bridge spans haystack sessions).
        max_seed_hits: How many of ``initial_hits`` to draw entities
            from. The top hits are most likely to be on-topic; lower
            hits introduce noise.
        max_seed_entities: Cap on distinct entities used as seeds.
        max_neighbor_facts_per_entity: Cap on facts fetched per seed
            entity.
        max_expanded_facts: Total cap on the returned expanded set.

    Returns:
        A list of ``PageIndexFactHit`` that are NOT in ``initial_hits``
        (deduped by ``fact_id``) and that mention at least one entity
        shared with the seed hits' top entities. Order matches the
        store's per-entity ranking; downstream rerank reorders.
    """
    if not initial_hits:
        return []

    # 1. Collect seed entities from the top initial hits. Preserve
    #    order of first appearance so deterministic tests are easy.
    seed_entities: list[str] = []
    seen_entities: set[str] = set()
    for hit in initial_hits[:max_seed_hits]:
        for entity in (hit.entities or []):
            key = entity.lower()
            if key in seen_entities:
                continue
            seen_entities.add(key)
            seed_entities.append(entity)
            if len(seed_entities) >= max_seed_entities:
                break
        if len(seed_entities) >= max_seed_entities:
            break

    if not seed_entities:
        return []

    # 2. For each seed entity, fetch facts that mention it. Dedup
    #    against the initial hits and against each other by fact_id.
    initial_ids: set[str] = {h.fact_id for h in initial_hits}
    expanded: list[PageIndexFactHit] = []
    expanded_ids: set[str] = set()

    for entity in seed_entities:
        if len(expanded) >= max_expanded_facts:
            break
        try:
            neighbor_hits = await store.search_facts_by_entity(
                bank_id, entity,
                top_k=max_neighbor_facts_per_entity,
                document_id=document_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "search_facts_by_entity(%r) failed: %s: %s",
                entity, type(exc).__name__, exc,
            )
            continue

        for hit in neighbor_hits:
            if hit.fact_id in initial_ids or hit.fact_id in expanded_ids:
                continue
            expanded.append(hit)
            expanded_ids.add(hit.fact_id)
            if len(expanded) >= max_expanded_facts:
                break

    return expanded
