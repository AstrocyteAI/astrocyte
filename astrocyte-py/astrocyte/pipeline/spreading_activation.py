"""Fix 3 (conv-run-4) — entity spreading activation at retrieval time.

After ``section_recall`` produces an initial top-K of fused hits, we
expand by one hop through entity co-occurrence: every retrieved
section's entities become a probe for other sections in the same bank
that share at least one of those entities. The expanded candidates
are appended to the fused list BEFORE the cross-encoder rerank, so the
rerank sees the full neighborhood and can promote a correct section
that the initial strategies missed.

Why this exists: the failure case is Denver/Disneyland conflation in
LME — the initial top-K surfaces "Disneyland" because it shares
keywords with "Denver" via a noisy LLM-extracted graph edge, but the
correct session ("Red Rocks", which is also in Denver) is not
keyword-adjacent to the question. Both sections share the entity
"Denver" though, so entity co-occurrence bridges them.

Distinct from ``expand_section_links`` (which uses the
``astrocyte_pi_section_links`` table populated by semantic_knn +
LLM-extracted edges): the link table is sparse in conversation ingest,
so graph_expand can't bridge entity-coincident sections. Entity spread
uses the dense ``astrocyte_pi_section_entities`` table that retain
already populates per section.

See:
- ``docs/_design/recall.md`` §6 (recall pipeline)
- ``astrocyte.pipeline.section_recall``
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.provider import PageIndexStore

_logger = logging.getLogger("astrocyte.pipeline.spreading_activation")


async def expand_via_shared_entities(
    *,
    store: PageIndexStore,
    bank_id: str,
    seeds: list[tuple[str, int]],
    top_k: int = 20,
    max_seeds: int = 10,
    exclude_seeds: bool = True,
) -> list[tuple[str, int, float]]:
    """One-hop entity-co-occurrence spread from a list of seed sections.

    Args:
        store: PageIndexStore SPI handle.
        bank_id: Scope to the user's bank.
        seeds: ``(document_id, line_num)`` pairs — typically the top
            ``recall.fused`` hits.
        top_k: Maximum number of expanded sections to return.
        max_seeds: Cap the seed count to keep the SQL bounded; the
            top entries in a fused list carry the strongest recall
            signal, so trimming the tail rarely costs precision.
        exclude_seeds: When True (default), filter the seeds themselves
            out of the result — the caller already has them in
            ``recall.fused`` and doesn't want duplicates.

    Returns:
        ``[(document_id, line_num, score), ...]`` where score is the
        count of distinct shared entities with any seed. Empty list
        on store errors or when the store doesn't implement the
        ``expand_sections_by_shared_entities`` SPI (older test fixtures).
    """
    if not seeds:
        return []
    if len(seeds) > max_seeds:
        seeds = seeds[:max_seeds]
    expander = getattr(store, "expand_sections_by_shared_entities", None)
    if expander is None:
        _logger.debug(
            "spreading_activation: store=%s has no expand_sections_by_shared_entities, skip",
            type(store).__name__,
        )
        return []
    try:
        return await expander(
            bank_id,
            seeds,
            top_k=top_k,
            exclude_seeds=exclude_seeds,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "spreading_activation: bank=%s seeds=%d failed (%s)",
            bank_id,
            len(seeds),
            exc,
        )
        return []
