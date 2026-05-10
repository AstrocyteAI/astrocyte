"""Section cross-encoder rerank (M9 PR2 commit C).

Sits between the section recall orchestrator (RRF-fused candidates) and
the picker (PageIndex's reasoning loop). Two responsibilities:

1. **Cross-encoder rerank**: take the top-30 RRF-fused hits, score each
   against the question with a cross-encoder, return the top-15. Same
   pattern Hindsight uses (``cross-encoder/ms-marco-MiniLM-L-6-v2``);
   we reuse the existing ``astrocyte.pipeline.cross_encoder_rerank``
   plumbing rather than reinventing it.

2. **Picker-as-reranker constraint**: build a "constrained skeleton" —
   the picker still sees a nested-dict tree, but only the 15 reranked
   nodes appear (other nodes are pruned). This is the structural fix
   for the v6 picker non-compliance: gpt-4o-mini reliably picks 5-10
   from a curated 15 but degenerates to ``[1]`` when given the raw
   30-node skeleton (proven in Phase A failure analysis).

The picker's prompt format is unchanged — it just sees fewer nodes.
No prompt tuning required. This is the cheapest accuracy lift in PR2.

See:
- ``docs/_design/recall.md`` §6, §8.3
- ``docs/_design/adr/adr-006-three-layer-recall-stack.md``
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from astrocyte.pipeline.cross_encoder_rerank import (
    CrossEncoderProtocol,
    cross_encoder_rerank,
)
from astrocyte.pipeline.reranking import ScoredItem

if TYPE_CHECKING:
    from astrocyte.pipeline.section_recall import FusedHit
    from astrocyte.types import PageIndexSection

logger = logging.getLogger("astrocyte.pipeline.section_rerank")


def rerank_fused_hits(
    fused: list[FusedHit],
    sections_by_key: dict[tuple[str, int], PageIndexSection],
    question: str,
    *,
    model: CrossEncoderProtocol | None = None,
    rerank_top_k: int = 30,
    output_top_k: int = 15,
) -> list[FusedHit]:
    """Rerank top-K fused hits with a cross-encoder; return top-N.

    Args:
      fused: ``SectionRecallResult.fused`` (sorted by RRF score desc).
      sections_by_key: Map ``(document_id, line_num) → PageIndexSection``
        so we can fetch (title + summary) without re-querying the store.
        Caller pre-builds this from the conv_tree's skeleton.
      question: User question, fed to the cross-encoder.
      model: Cross-encoder; ``None`` → default Hindsight model.
      rerank_top_k: How many of the RRF top to actually rescore. The
        cross-encoder is the slow part (transformer inference); we cap
        at 30 by default. Items beyond this rank pass through with
        their original RRF score.
      output_top_k: Final length of the returned list.

    Returns:
      ``FusedHit`` list, sorted by cross-encoder score descending,
      truncated to ``output_top_k``. The ``rrf_score`` field is
      replaced with the cross-encoder score for transparency.
    """
    if not fused:
        return []

    head = fused[:rerank_top_k]
    items = []
    for h in head:
        section = sections_by_key.get((h.document_id, h.line_num))
        # Build the rerank input text from title + summary. The body
        # itself isn't needed at this stage; the picker fetches
        # excerpts for the synth, not the reranker.
        if section is None:
            text = f"line {h.line_num}"
        else:
            title = section.title or ""
            summary = section.summary or ""
            text = f"{title}. {summary}".strip(" .")
        items.append(
            ScoredItem(
                id=f"{h.document_id}:{h.line_num}",
                text=text,
                score=h.rrf_score,
            )
        )

    rescored = cross_encoder_rerank(items, question, model=model)

    # Map ScoredItem.id back to (doc, line) and emit FusedHit objects
    # with the new score in ``rrf_score`` (we abuse the field for
    # uniformity downstream — picker doesn't care which scorer wrote it).
    from astrocyte.pipeline.section_recall import FusedHit  # avoid circular import

    out: list[FusedHit] = []
    by_key = {(h.document_id, h.line_num): h for h in fused}
    for item in rescored[:output_top_k]:
        doc_id, line_str = item.id.split(":", 1)
        line_num = int(line_str)
        original = by_key.get((doc_id, line_num))
        if original is None:
            continue
        out.append(
            FusedHit(
                document_id=doc_id,
                line_num=line_num,
                rrf_score=float(item.score),
                per_strategy_rank=dict(original.per_strategy_rank),
            )
        )
    return out


def build_constrained_skeleton(
    full_skeleton: list | dict,
    keep_keys: set[tuple[str, int]],
    document_id: str,
) -> list:
    """Prune the picker's nested-dict skeleton to only the nodes in
    ``keep_keys`` (preserving tree structure).

    The picker's prompt format expects a nested-dict tree; the skeleton
    we pass in PR1 was the FULL tree (~30 nodes for LoCoMo). PR2 commit
    C narrows that to the top-15 reranked sections so the picker has a
    much smaller, more relevant input.

    A node is kept if EITHER:
      a) ``(document_id, node.line_num)`` is in ``keep_keys``, OR
      b) it has at least one descendant that is in ``keep_keys``
         (so the path from root to a kept leaf survives — the picker's
         tree-walk needs the parent chain).

    Returns a list of root-level nodes. The caller passes this to the
    picker the same way it would pass the full skeleton.
    """

    def _walk(node: dict) -> dict | None:
        kept = (document_id, node.get("line_num")) in keep_keys
        children = node.get("nodes")
        kept_children: list[dict] = []
        if isinstance(children, list):
            for child in children:
                if not isinstance(child, dict):
                    continue
                pruned = _walk(child)
                if pruned is not None:
                    kept_children.append(pruned)
        if not kept and not kept_children:
            return None
        out = {k: v for k, v in node.items() if k != "nodes"}
        if kept_children:
            out["nodes"] = kept_children
        return out

    if isinstance(full_skeleton, dict):
        full_skeleton = full_skeleton.get("structure", [full_skeleton])

    pruned: list[dict] = []
    for node in full_skeleton:
        if not isinstance(node, dict):
            continue
        kept = _walk(node)
        if kept is not None:
            pruned.append(kept)
    return pruned
