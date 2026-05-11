"""M12.3: Cross-encoder rerank over fact-grain hits.

Sits between fact retrieval (semantic / entity / temporal) and the
``[FACTS]`` block in the synth prompt. Mirrors
``astrocyte.pipeline.section_rerank.rerank_fused_hits`` — same cross-
encoder backend, same module-level cache, same pattern of building a
text representation per candidate and calling
``cross_encoder_rerank``.

Why a separate module:

- Facts have different metadata (fact_type, speaker, entities,
  occurred_*) and a richer rerank-input text could in principle attend
  to those. The v1 keeps it minimal: just ``fact.text``. The MS MARCO
  cross-encoder is trained on natural-language passages; injecting
  structured metadata as `[key=value]` tokens tends to confuse it.
- Facts are typically retrieved from a wider pool (top-30+ semantic)
  and then narrowed by a picker-line filter. Reranking is cheapest
  when applied to the already-filtered subset.

Generic across benches — the cross-encoder doesn't know LME from
LoCoMo, and the rerank text contains no bench-specific shaping.

See:
- ``docs/_design/benchmark-comparison-methodology.md`` for harness rules
- ``astrocyte.pipeline.section_rerank`` for the section-grain analogue
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from astrocyte.pipeline.cross_encoder_rerank import (
    CrossEncoderProtocol,
    cross_encoder_rerank,
)
from astrocyte.pipeline.reranking import ScoredItem

if TYPE_CHECKING:
    from astrocyte.types import PageIndexFactHit

logger = logging.getLogger("astrocyte.pipeline.fact_rerank")


def rerank_fact_hits(
    hits: list[PageIndexFactHit],
    question: str,
    *,
    model: CrossEncoderProtocol | None = None,
    rerank_top_k: int = 30,
    output_top_k: int = 12,
) -> list[PageIndexFactHit]:
    """Cross-encoder rerank ``hits`` against ``question``.

    Args:
        hits: Fact hits, typically the union of semantic / entity /
            temporal search results, already deduped by ``fact_id``.
            Order on input doesn't matter — the cross-encoder reorders
            from scratch.
        question: User question, fed to the cross-encoder.
        model: Cross-encoder backend. ``None`` → cached default
            (``cross-encoder/ms-marco-MiniLM-L-6-v2``).
        rerank_top_k: Cap on how many candidates to actually rescore.
            Cross-encoder inference is the slow part; we bound it at 30
            by default. Items beyond this rank pass through with their
            original score.
        output_top_k: Final length of the returned list (post-rerank).

    Returns:
        Hits sorted by cross-encoder score descending, truncated to
        ``output_top_k``. The ``score`` field is replaced with the
        cross-encoder score for transparency downstream.
    """
    if not hits:
        return []

    head = hits[:rerank_top_k]
    items = [
        ScoredItem(
            id=h.fact_id,
            text=h.text,
            score=h.score,
        )
        for h in head
    ]

    rescored = cross_encoder_rerank(items, question, model=model)

    by_id = {h.fact_id: h for h in head}
    out: list[PageIndexFactHit] = []
    for item in rescored[:output_top_k]:
        original = by_id.get(item.id)
        if original is None:
            continue
        # ``replace`` shallow-copies the dataclass with the new score,
        # automatically picking up any future PageIndexFactHit fields
        # added to types.py. The shallow-copy semantics match the rest
        # of the codebase's treatment of dataclass-like hits.
        out.append(replace(original, score=float(item.score)))
    return out
