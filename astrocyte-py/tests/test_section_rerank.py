"""Tests for ``astrocyte.pipeline.section_rerank`` (M9 PR2 commit C).

Two public functions, two test classes:

1. ``TestRerankFusedHits`` — pin the cross-encoder rerank contract:
   empty input, reorder by score, ``rerank_top_k`` caps inference,
   ``output_top_k`` caps output, metadata preservation, fallback when
   a section is missing from the lookup map (text becomes
   ``"line N"``).

2. ``TestBuildConstrainedSkeleton`` — pin the picker-as-reranker
   structural pruning: only nodes in ``keep_keys`` (and their parent
   chains) survive, accepting both list and dict-with-``structure``
   inputs. The picker's nested-dict tree shape must round-trip.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.cross_encoder_rerank import CrossEncoderProtocol
from astrocyte.pipeline.section_recall import FusedHit
from astrocyte.pipeline.section_rerank import (
    build_constrained_skeleton,
    rerank_fused_hits,
)
from astrocyte.types import PageIndexSection

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeCrossEncoder:
    """Deterministic cross-encoder used in tests — same shape as the
    fact_rerank test fixture."""

    def __init__(self, scores_by_text: dict[str, float]) -> None:
        self.scores_by_text = scores_by_text
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, candidates: list[str]) -> list[float]:
        self.calls.append((query, list(candidates)))
        return [self.scores_by_text.get(t, 0.0) for t in candidates]


def _hit(doc_id: str, line_num: int, rrf: float) -> FusedHit:
    return FusedHit(
        document_id=doc_id,
        line_num=line_num,
        rrf_score=rrf,
        per_strategy_rank={"semantic": 1, "keyword": 2},
    )


def _section(doc_id: str, line_num: int, title: str, summary: str = "") -> PageIndexSection:
    return PageIndexSection(
        document_id=doc_id,
        line_num=line_num,
        node_id=f"{line_num:04d}",
        title=title,
        summary=summary,
        depth=1,
    )


# ---------------------------------------------------------------------------
# rerank_fused_hits — cross-encoder rerank of RRF-fused section hits
# ---------------------------------------------------------------------------


class TestRerankFusedHits:
    def test_empty_input_returns_empty(self) -> None:
        assert rerank_fused_hits([], {}, "any question") == []

    def test_reorders_by_cross_encoder_score(self) -> None:
        """The reranker must replace the RRF order with the cross-encoder
        order. ``rrf_score`` is overwritten with the new score so the
        picker can read a single score field uniformly."""
        hits = [
            _hit("d1", 1, rrf=0.9),
            _hit("d1", 2, rrf=0.5),
            _hit("d1", 3, rrf=0.1),
        ]
        sections = {
            ("d1", 1): _section("d1", 1, "apples", "the user likes apples"),
            ("d1", 2): _section("d1", 2, "doctor", "the user visited Dr. Patel"),
            ("d1", 3): _section("d1", 3, "camera", "user prefers Sony cameras"),
        }
        fake = FakeCrossEncoder({
            "apples. the user likes apples": 0.1,
            "doctor. the user visited Dr. Patel": 0.9,
            "camera. user prefers Sony cameras": 0.5,
        })
        out = rerank_fused_hits(
            hits, sections, "what doctor did the user see?",
            model=fake, rerank_top_k=10, output_top_k=10,
        )
        assert [(h.document_id, h.line_num) for h in out] == [
            ("d1", 2), ("d1", 3), ("d1", 1),
        ]
        # rrf_score is overwritten with the cross-encoder score.
        assert out[0].rrf_score == 0.9
        assert out[1].rrf_score == 0.5
        assert out[2].rrf_score == 0.1

    def test_truncates_to_output_top_k(self) -> None:
        hits = [_hit("d1", i, rrf=1.0 - i * 0.1) for i in range(5)]
        sections = {
            ("d1", i): _section("d1", i, f"t{i}", f"s{i}")
            for i in range(5)
        }
        fake = FakeCrossEncoder({
            f"t{i}. s{i}": float(i) for i in range(5)
        })
        out = rerank_fused_hits(
            hits, sections, "q",
            model=fake, rerank_top_k=10, output_top_k=2,
        )
        # text 4 (score 4.0) and text 3 (score 3.0) are the top two.
        assert len(out) == 2
        assert [h.line_num for h in out] == [4, 3]

    def test_rerank_top_k_caps_rescored_items(self) -> None:
        """Only the first ``rerank_top_k`` get sent to the cross-encoder.
        Items beyond that rank don't participate in the rerank at all."""
        hits = [_hit("d1", i, rrf=1.0 - i * 0.01) for i in range(10)]
        sections = {
            ("d1", i): _section("d1", i, f"t{i}", f"s{i}")
            for i in range(10)
        }
        fake = FakeCrossEncoder({f"t{i}. s{i}": 1.0 for i in range(10)})
        rerank_fused_hits(
            hits, sections, "q",
            model=fake, rerank_top_k=3, output_top_k=10,
        )
        # Only the top-3 by RRF should have been sent to the cross-encoder.
        assert len(fake.calls) == 1
        _query, candidates = fake.calls[0]
        assert len(candidates) == 3
        assert candidates == ["t0. s0", "t1. s1", "t2. s2"]

    def test_preserves_per_strategy_rank(self) -> None:
        """The reranker doesn't know which strategies contributed; it
        just preserves ``per_strategy_rank`` from the input fused hit so
        downstream tracing keeps working."""
        hits = [_hit("d1", 1, rrf=0.5)]
        hits[0].per_strategy_rank = {"semantic": 3, "keyword": 7, "entity": 1}
        sections = {("d1", 1): _section("d1", 1, "t", "s")}
        fake = FakeCrossEncoder({"t. s": 0.9})

        out = rerank_fused_hits(hits, sections, "q", model=fake)

        assert len(out) == 1
        assert out[0].per_strategy_rank == {"semantic": 3, "keyword": 7, "entity": 1}
        # Defensive copy: mutating the input doesn't bleed into the output.
        hits[0].per_strategy_rank["semantic"] = 99
        assert out[0].per_strategy_rank["semantic"] == 3

    def test_missing_section_falls_back_to_line_text(self) -> None:
        """When a hit's (doc, line) isn't in ``sections_by_key``, the
        rerank input falls back to ``"line N"``. Mirrors the bench's
        production path where the section map can lag the fused hits
        (e.g. when graph_expand surfaces a section the picker hasn't
        loaded yet)."""
        hits = [_hit("d1", 42, rrf=0.5)]
        sections: dict = {}  # intentionally empty
        fake = FakeCrossEncoder({"line 42": 0.7})

        out = rerank_fused_hits(hits, sections, "q", model=fake)

        assert len(out) == 1
        assert out[0].rrf_score == 0.7
        # The fake recorded the fallback text.
        assert fake.calls[0][1] == ["line 42"]

    def test_section_with_title_no_summary(self) -> None:
        """A section with title but no summary still produces a valid
        rerank text (no trailing ' .')."""
        hits = [_hit("d1", 1, rrf=0.5)]
        sections = {("d1", 1): _section("d1", 1, "Just A Title", "")}
        fake = FakeCrossEncoder({"Just A Title": 0.7})

        out = rerank_fused_hits(hits, sections, "q", model=fake)

        # The rerank text strips the trailing period+space.
        assert fake.calls[0][1] == ["Just A Title"]
        assert len(out) == 1

    def test_protocol_compliance(self) -> None:
        """FakeCrossEncoder satisfies CrossEncoderProtocol structurally."""
        fake = FakeCrossEncoder({"a": 1.0})
        assert isinstance(fake, CrossEncoderProtocol)


# ---------------------------------------------------------------------------
# build_constrained_skeleton — picker-as-reranker tree pruning
# ---------------------------------------------------------------------------


class TestBuildConstrainedSkeleton:
    """Prune the picker's nested-dict skeleton so only ``keep_keys``
    nodes (and their parent chains) survive."""

    def _flat_tree(self) -> list:
        """3 leaf sections at depth 1, no nesting."""
        return [
            {"line_num": 1, "title": "S1", "summary": "s1"},
            {"line_num": 2, "title": "S2", "summary": "s2"},
            {"line_num": 3, "title": "S3", "summary": "s3"},
        ]

    def _nested_tree(self) -> list:
        """One parent (depth 0) with three leaves (depth 1)."""
        return [
            {
                "line_num": 0,
                "title": "Root",
                "nodes": [
                    {"line_num": 1, "title": "S1"},
                    {"line_num": 2, "title": "S2"},
                    {"line_num": 3, "title": "S3"},
                ],
            },
        ]

    def test_filters_flat_tree_to_keep_keys(self) -> None:
        keep = {("d1", 1), ("d1", 3)}
        pruned = build_constrained_skeleton(self._flat_tree(), keep, "d1")
        assert [n["line_num"] for n in pruned] == [1, 3]
        # Pruned nodes carry their original fields (modulo ``nodes``).
        assert pruned[0]["title"] == "S1"
        assert pruned[0]["summary"] == "s1"

    def test_keeps_parent_for_kept_descendant(self) -> None:
        """A parent must survive if it has at least one descendant in
        keep_keys — otherwise the picker's tree-walk loses the path."""
        keep = {("d1", 2)}  # leaf only — parent (line 0) is NOT in keep
        pruned = build_constrained_skeleton(self._nested_tree(), keep, "d1")
        assert len(pruned) == 1
        parent = pruned[0]
        assert parent["line_num"] == 0
        # Only the kept descendant survives under the parent.
        assert len(parent["nodes"]) == 1
        assert parent["nodes"][0]["line_num"] == 2

    def test_drops_subtree_with_no_keep(self) -> None:
        """A node with no surviving descendants and not in keep_keys
        itself is dropped entirely."""
        keep = {("d1", 999)}  # nothing in the tree matches
        pruned = build_constrained_skeleton(self._nested_tree(), keep, "d1")
        assert pruned == []

    def test_document_id_scoping(self) -> None:
        """``keep_keys`` from a different document don't keep our nodes."""
        # keep_keys names line 1, but for document "other-doc".
        keep = {("other-doc", 1)}
        pruned = build_constrained_skeleton(self._flat_tree(), keep, "d1")
        assert pruned == []

    def test_accepts_dict_with_structure_key(self) -> None:
        """The bench passes the conv_tree dict directly, not just the
        list. The function unwraps ``tree["structure"]`` automatically."""
        tree_dict = {
            "reference_date": "2026-05-01",
            "structure": self._flat_tree(),
        }
        keep = {("d1", 2)}
        pruned = build_constrained_skeleton(tree_dict, keep, "d1")
        assert [n["line_num"] for n in pruned] == [2]

    def test_non_dict_nodes_are_skipped(self) -> None:
        """Garbage entries in the skeleton (e.g. None, str) are filtered
        out — defensive against malformed tree JSON."""
        garbage_tree = [
            None,
            "not-a-dict",
            42,
            {"line_num": 1, "title": "S1"},
        ]
        keep = {("d1", 1)}
        pruned = build_constrained_skeleton(garbage_tree, keep, "d1")
        assert len(pruned) == 1
        assert pruned[0]["line_num"] == 1

    def test_garbage_children_are_skipped(self) -> None:
        """Same defensive filter inside nested nodes."""
        tree = [
            {
                "line_num": 0,
                "title": "Root",
                "nodes": [
                    None,
                    {"line_num": 1, "title": "S1"},
                    "garbage",
                ],
            },
        ]
        keep = {("d1", 1)}
        pruned = build_constrained_skeleton(tree, keep, "d1")
        assert len(pruned) == 1
        # Garbage children dropped; valid child kept.
        assert len(pruned[0]["nodes"]) == 1
        assert pruned[0]["nodes"][0]["line_num"] == 1

    def test_node_in_keep_with_no_children_emits_no_nodes_key(self) -> None:
        """When a kept node has no kept descendants, the output dict
        shouldn't include an empty ``nodes`` list — the picker's prompt
        format treats absence and emptiness differently."""
        keep = {("d1", 1)}
        pruned = build_constrained_skeleton(self._flat_tree(), keep, "d1")
        assert "nodes" not in pruned[0]

    def test_empty_skeleton_returns_empty(self) -> None:
        assert build_constrained_skeleton([], {("d1", 1)}, "d1") == []

    @pytest.mark.parametrize(
        "tree",
        [
            [],                                  # empty list
            {"structure": []},                   # dict with empty structure
        ],
    )
    def test_empty_inputs_return_empty(self, tree) -> None:
        assert build_constrained_skeleton(tree, {("d1", 1)}, "d1") == []
