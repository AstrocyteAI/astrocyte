"""Unit tests for the Hindsight-parity cross-encoder reranker.

The production backend (``SentenceTransformersCrossEncoder``) requires
``sentence-transformers`` + ``torch`` and downloads a model from
HuggingFace on first use, which makes it unsuitable for unit tests.
These tests exercise the reranking logic with a deterministic fake that
implements :class:`CrossEncoderProtocol`.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.cross_encoder_rerank import (
    CrossEncoderProtocol,
    cross_encoder_rerank,
    get_default_cross_encoder,
    reset_default_cross_encoder_cache,
)
from astrocyte.pipeline.fusion import ScoredItem


class _FakeCrossEncoder:
    """Records call args and returns a configurable score vector."""

    def __init__(self, scores_by_text: dict[str, float]) -> None:
        self._scores = scores_by_text
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, candidates: list[str]) -> list[float]:
        self.calls.append((query, list(candidates)))
        # Default to 0.0 for any text not pre-mapped — lets tests assert
        # exact ranking without enumerating every candidate.
        return [self._scores.get(c, 0.0) for c in candidates]


def _item(item_id: str, text: str, score: float = 0.0) -> ScoredItem:
    return ScoredItem(id=item_id, text=text, score=score)


class TestCrossEncoderRerank:
    def test_empty_inputs_passthrough(self):
        """No work to do when items or query is empty."""
        fake = _FakeCrossEncoder({})
        assert cross_encoder_rerank([], "q", model=fake) == []
        assert cross_encoder_rerank([_item("a", "x")], "", model=fake) == [
            _item("a", "x")
        ]
        assert fake.calls == []

    def test_reranks_by_cross_encoder_score(self):
        """Items end up in descending cross-encoder score order — even
        when that order disagrees with their incoming bi-encoder score."""
        fake = _FakeCrossEncoder({
            "alice loves pottery": 0.95,
            "bob runs trails":     0.10,
            "carol bakes bread":   0.50,
        })
        items = [
            _item("a", "alice loves pottery", score=0.20),  # was last by score
            _item("b", "bob runs trails",     score=0.90),  # was first
            _item("c", "carol bakes bread",   score=0.50),
        ]

        ranked = cross_encoder_rerank(items, "Who likes pottery?", model=fake)

        assert [it.id for it in ranked] == ["a", "c", "b"]
        # Original metadata fields preserved; scores replaced by CE scores.
        assert ranked[0].score == pytest.approx(0.95)
        assert ranked[1].score == pytest.approx(0.50)
        assert ranked[2].score == pytest.approx(0.10)

    def test_top_k_only_rescored_remainder_appended(self):
        """``top_k`` bounds the cross-encoder call to the head of the
        list. Items past ``top_k`` keep their original score and follow
        the reranked head in the original relative order."""
        fake = _FakeCrossEncoder({
            "head-A": 0.30,  # ranked 2nd in head
            "head-B": 0.80,  # ranked 1st in head
        })
        items = [
            _item("a", "head-A", score=0.50),
            _item("b", "head-B", score=0.40),
            _item("c", "tail-1", score=0.30),
            _item("d", "tail-2", score=0.20),
        ]

        ranked = cross_encoder_rerank(items, "q", model=fake, top_k=2)

        # Only the head was sent to the model.
        assert fake.calls == [("q", ["head-A", "head-B"])]
        # head reranked by CE score; tail follows in original order.
        assert [it.id for it in ranked] == ["b", "a", "c", "d"]
        # Tail items kept their original bi-encoder scores.
        assert ranked[2].score == pytest.approx(0.30)
        assert ranked[3].score == pytest.approx(0.20)

    def test_preserves_item_metadata(self):
        """Reranking must not strip fact_type / metadata / tags."""
        fake = _FakeCrossEncoder({"alpha": 1.0})
        items = [
            ScoredItem(
                id="a",
                text="alpha",
                score=0.1,
                fact_type="experience",
                metadata={"k": "v"},
                tags=["t1"],
                memory_layer="raw",
            )
        ]

        ranked = cross_encoder_rerank(items, "q", model=fake)

        assert ranked[0].fact_type == "experience"
        assert ranked[0].metadata == {"k": "v"}
        assert ranked[0].tags == ["t1"]
        assert ranked[0].memory_layer == "raw"


class TestModelCache:
    def test_get_default_returns_same_instance_for_same_args(self):
        """Module-level cache returns the same instance for repeat calls
        — avoids reloading the (~80 MB) model per reflect."""
        reset_default_cross_encoder_cache()
        try:
            a = get_default_cross_encoder("model-x", force_cpu=False)
            b = get_default_cross_encoder("model-x", force_cpu=False)
            assert a is b
        finally:
            reset_default_cross_encoder_cache()

    def test_get_default_segregates_by_force_cpu(self):
        """Different ``force_cpu`` values get separate cache entries —
        a CPU-pinned and a default-device instance must not share state."""
        reset_default_cross_encoder_cache()
        try:
            cpu = get_default_cross_encoder("model-x", force_cpu=True)
            gpu = get_default_cross_encoder("model-x", force_cpu=False)
            assert cpu is not gpu
        finally:
            reset_default_cross_encoder_cache()


class TestProtocolConformance:
    def test_fake_satisfies_protocol_runtime_check(self):
        """The fake used in tests must pass ``isinstance`` against the
        runtime-checkable protocol — guards the production interface."""
        fake = _FakeCrossEncoder({})
        assert isinstance(fake, CrossEncoderProtocol)
