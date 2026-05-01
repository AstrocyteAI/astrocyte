"""3-parallel-signal link expansion (Hindsight parity, C3b).

This is the C3 rewrite of ``spreading_activation.py``. The previous
module did a BFS hop walk over ``co_occurs`` entity edges; Hindsight's
``link_expansion_retrieval.py`` doesn't walk multi-hop entity chains
that way. Instead, it queries three first-class link signals in
parallel and combines them:

1. **Entity overlap** — query-time set-overlap. Candidate memories
   that share entities with the seeds score by ``count(distinct shared
   entities)``. Computed here in Python via the
   ``GraphStore.get_entity_ids_for_memories`` SPI plus a reverse map
   (entity → memories) materialized on the fly.

2. **Semantic links** — precomputed at retain time
   (:mod:`semantic_link_graph`). Edges of type ``"semantic"`` connect
   each new memory to its top-K most-similar neighbors. The
   link-expansion query reads these directly from
   ``GraphStore.find_memory_links``.

3. **Causal links** — explicit ``"caused_by"`` chains extracted at
   retain time (:mod:`fact_causal_extraction`). Boosted (+1.0 weight)
   as the highest-quality signal because the source-text causal
   evidence is unambiguous.

Hindsight's actual implementation runs all three as a single
recursive-CTE Postgres query for speed. We do the same shape in
Python because the orchestrator's :class:`GraphStore` SPI must work
for arbitrary backends (in-memory tests, AGE, future stores). For
LoCoMo-scale workloads (~thousands of memories per bank), the Python
path is well within latency budget.

The return type is ``list[ScoredItem]`` — same as the old
spread_activation function — so the orchestrator's RRF-fusion
plumbing slots in without changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from astrocyte.pipeline.fusion import ScoredItem
from astrocyte.provider import GraphStore, VectorStore

_logger = logging.getLogger("astrocyte.link_expansion")


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


@dataclass
class LinkExpansionParams:
    """Tunable knobs for the 3-signal expansion (Hindsight parity).

    Defaults match Hindsight's published configuration where stated;
    score weights mirror their reranking blend (entity overlap, semantic
    weight, causal +1.0 boost).
    """

    expansion_limit: int = 30
    #: Per-entity LATERAL cap (Hindsight's ``graph_per_entity_limit``):
    #: when an entity is shared with many candidates, take at most this
    #: many of them per entity to prevent fanout explosion.
    per_entity_limit: int = 200
    #: Score weights — each signal is normalized to [0, 1] before the
    #: weighted sum. Causal gets the highest weight per Hindsight's note
    #: that ``causes`` chains are the highest-precision signal.
    entity_overlap_weight: float = 0.5
    semantic_weight: float = 0.3
    causal_weight: float = 0.7
    #: Causal link types to walk. Currently only ``caused_by`` is
    #: extracted at retain time, but reserved for future extensions
    #: (``enables``, ``prevents``, etc.).
    causal_link_types: tuple[str, ...] = ("caused_by",)
    semantic_link_types: tuple[str, ...] = ("semantic",)
    #: Minimum total score (post-weighting) for a candidate to surface.
    activation_threshold: float = 0.05


# ---------------------------------------------------------------------------
# Tag scope helper (mirrors spread/expand path)
# ---------------------------------------------------------------------------


def _hit_has_required_tags(
    metadata: dict | None,
    tags: list[str] | None,
    required_tags: set[str],
) -> bool:
    if not required_tags:
        return True
    item_tags = {str(t).lower() for t in (tags or [])}
    return required_tags.issubset(item_tags)


# ---------------------------------------------------------------------------
# Score accumulator
# ---------------------------------------------------------------------------


@dataclass
class _CandidateScore:
    memory_id: str
    entity_overlap: int = 0  # count of distinct shared entities
    semantic_total: float = 0.0  # sum of semantic edge weights
    causal_total: float = 0.0  # sum of (causal weight + 1.0) per Hindsight
    sources: set[str] = field(default_factory=set)  # which signals contributed

    def total(self, params: LinkExpansionParams) -> float:
        # Normalize entity overlap by a small constant — diminishing
        # returns past 5 shared entities. Hindsight uses raw count;
        # the normalization here keeps the weighted sum interpretable.
        eo_norm = min(1.0, self.entity_overlap / 5.0)
        sem_norm = min(1.0, self.semantic_total)
        causal_norm = min(1.0, self.causal_total)
        return (
            params.entity_overlap_weight * eo_norm
            + params.semantic_weight * sem_norm
            + params.causal_weight * causal_norm
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def link_expansion(
    seed_hits: list[ScoredItem],
    *,
    bank_id: str,
    vector_store: VectorStore,
    graph_store: GraphStore,
    params: LinkExpansionParams | None = None,
    tags: list[str] | None = None,
) -> list[ScoredItem]:
    """Expand seeds through the three first-class memory-link signals.

    Returns NEW candidate memories only (seeds are filtered out). Each
    return ``ScoredItem`` carries metadata explaining which signal(s)
    surfaced it:

    - ``_link_signal``: comma-separated list of contributing signals
      (``entity_overlap``, ``semantic``, ``causal``).
    - ``_entity_overlap_count``: how many entities it shares with seeds.
    - ``_semantic_weight_total``: sum of semantic edge weights to seeds.
    - ``_causal_weight_total``: sum of causal edge weights to seeds.

    Args:
        seed_hits: Top-K from initial RRF fusion. Their entity IDs and
            memory_ids drive all three signal queries.
        bank_id: Constrains every query.
        vector_store: Used to hydrate full memory bodies after scoring.
        graph_store: Source of all three signals via the optional
            ``get_entity_ids_for_memories`` and ``find_memory_links``
            methods. Returns ``[]`` early when either is unavailable.
        params: Tuning knobs; defaults match Hindsight.
        tags: Optional tag filter — candidates failing scope are
            dropped before being returned (LoCoMo's ``convo:<id>``
            scoping reuses this).
    """
    if not seed_hits:
        return []
    p = params or LinkExpansionParams()
    seed_ids = {h.id for h in seed_hits}
    required_tags = {str(t).lower() for t in tags} if tags else set()

    candidates: dict[str, _CandidateScore] = {}

    # --- Signal 1: entity overlap --------------------------------------
    # Pull entity associations for the seeds, then for each entity,
    # find its other memories. The reverse-lookup uses
    # ``query_neighbors`` since that's the existing memories↔entities
    # surface; the per-entity LATERAL cap mirrors Hindsight's
    # ``graph_per_entity_limit``.
    get_entities = getattr(graph_store, "get_entity_ids_for_memories", None)
    if get_entities is not None:
        try:
            seed_entity_map = await get_entities([h.id for h in seed_hits], bank_id)
        except Exception as exc:
            _logger.warning("entity-overlap lookup failed (%s)", exc)
            seed_entity_map = {}

        seed_entity_ids: set[str] = set()
        for ents in seed_entity_map.values():
            seed_entity_ids.update(ents)

        if seed_entity_ids:
            try:
                # Reverse: which other memories carry these entities?
                graph_hits = await graph_store.query_neighbors(
                    list(seed_entity_ids),
                    bank_id,
                    max_depth=1,
                    limit=p.per_entity_limit * len(seed_entity_ids),
                )
            except Exception as exc:
                _logger.warning("query_neighbors failed (%s)", exc)
                graph_hits = []

            # For each candidate memory, count distinct shared entities.
            for ghit in graph_hits:
                mid = ghit.memory_id
                if mid in seed_ids:
                    continue
                shared = set(ghit.connected_entities or []) & seed_entity_ids
                if not shared:
                    continue
                cand = candidates.setdefault(mid, _CandidateScore(memory_id=mid))
                cand.entity_overlap = max(cand.entity_overlap, len(shared))
                cand.sources.add("entity_overlap")

    # --- Signals 2 & 3: precomputed memory_links -----------------------
    find_links = getattr(graph_store, "find_memory_links", None)
    if find_links is not None:
        all_link_types = list(p.semantic_link_types) + list(p.causal_link_types)
        try:
            links = await find_links(
                [h.id for h in seed_hits], bank_id,
                link_types=all_link_types,
                limit=p.expansion_limit * 4,
            )
        except Exception as exc:
            _logger.warning("find_memory_links failed (%s)", exc)
            links = []

        for link in links:
            # The link's "other end" relative to a seed is what we want
            # to surface as a candidate.
            if link.source_memory_id in seed_ids and link.target_memory_id not in seed_ids:
                other = link.target_memory_id
            elif link.target_memory_id in seed_ids and link.source_memory_id not in seed_ids:
                other = link.source_memory_id
            else:
                continue

            cand = candidates.setdefault(other, _CandidateScore(memory_id=other))
            if link.link_type in p.semantic_link_types:
                cand.semantic_total += float(link.weight)
                cand.sources.add("semantic")
            elif link.link_type in p.causal_link_types:
                # Hindsight: causal weight + 1.0 boost.
                cand.causal_total += float(link.weight) + 1.0
                cand.sources.add("causal")

    if not candidates:
        return []

    # --- Hydrate bodies & filter --------------------------------------
    # Cap candidate set before fetching bodies to bound the cost.
    ranked = sorted(
        candidates.values(),
        key=lambda c: c.total(p),
        reverse=True,
    )
    ranked = [c for c in ranked if c.total(p) >= p.activation_threshold]
    ranked = ranked[: p.expansion_limit * 2]  # over-fetch; tag filter cuts later
    if not ranked:
        return []

    bodies = await _fetch_bodies_by_id(vector_store, bank_id, [c.memory_id for c in ranked])

    out: list[ScoredItem] = []
    for cand in ranked:
        body = bodies.get(cand.memory_id)
        if body is None:
            continue
        if not _hit_has_required_tags(body.metadata, body.tags, required_tags):
            continue

        metadata = dict(body.metadata or {})
        metadata["_link_signal"] = ",".join(sorted(cand.sources))
        if cand.entity_overlap > 0:
            metadata["_entity_overlap_count"] = cand.entity_overlap
        if cand.semantic_total > 0:
            metadata["_semantic_weight_total"] = round(cand.semantic_total, 4)
        if cand.causal_total > 0:
            metadata["_causal_weight_total"] = round(cand.causal_total, 4)

        out.append(
            ScoredItem(
                id=body.id,
                text=body.text,
                score=cand.total(p),
                fact_type=body.fact_type,
                metadata=metadata,
                tags=body.tags,
                memory_layer=body.memory_layer,
                occurred_at=body.occurred_at,
                retained_at=body.retained_at,
            )
        )
        if len(out) >= p.expansion_limit:
            break

    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_bodies_by_id(
    vector_store: VectorStore,
    bank_id: str,
    memory_ids: list[str],
):
    """Resolve memory IDs to their full ``VectorItem`` bodies.

    Bounded ``list_vectors`` scan; same pattern as
    ``PipelineOrchestrator._fetch_memory_hits_by_id``. For LoCoMo-scale
    banks this is fine; for very large banks we'd want a batched
    ``get_by_ids`` SPI extension.
    """
    target = set(memory_ids)
    out: dict[str, object] = {}
    offset = 0
    batch = 200
    while target:
        chunk = await vector_store.list_vectors(bank_id, offset=offset, limit=batch)
        if not chunk:
            break
        for item in chunk:
            if item.id in target:
                out[item.id] = item
                target.discard(item.id)
        if len(chunk) < batch:
            break
        offset += batch
    return out
