"""M12.3: fact-grain cross-encoder rerank unit tests."""
from __future__ import annotations

from datetime import datetime

from astrocyte.pipeline.cross_encoder_rerank import CrossEncoderProtocol
from astrocyte.pipeline.fact_rerank import rerank_fact_hits
from astrocyte.types import PageIndexFactHit


class FakeCrossEncoder:
    """Deterministic cross-encoder used in tests.

    ``score`` returns a hardcoded list keyed by candidate text — the
    test sets up the mapping and asserts the post-rerank order.
    """

    def __init__(self, scores_by_text: dict[str, float]) -> None:
        self.scores_by_text = scores_by_text
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, candidates: list[str]) -> list[float]:
        self.calls.append((query, list(candidates)))
        return [self.scores_by_text.get(t, 0.0) for t in candidates]


def _hit(fact_id: str, text: str, score: float = 0.5, line_num: int = 1) -> PageIndexFactHit:
    return PageIndexFactHit(
        fact_id=fact_id,
        document_id="doc-1",
        line_num=line_num,
        text=text,
        fact_type="experience",
        speaker="user",
        occurred_start=None,
        occurred_end=None,
        entities=["e1"],
        score=score,
    )


def test_empty_input_returns_empty() -> None:
    assert rerank_fact_hits([], "any question") == []


def test_reorders_by_cross_encoder_score() -> None:
    hits = [
        _hit("f1", "the user likes apples", score=0.9),
        _hit("f2", "the user visited Dr. Patel", score=0.8),
        _hit("f3", "user prefers Sony cameras", score=0.7),
    ]
    fake = FakeCrossEncoder({
        "the user likes apples": 0.1,
        "the user visited Dr. Patel": 0.9,
        "user prefers Sony cameras": 0.5,
    })
    out = rerank_fact_hits(
        hits, "what doctor did the user see?",
        model=fake, rerank_top_k=10, output_top_k=10,
    )
    assert [h.fact_id for h in out] == ["f2", "f3", "f1"]
    # New scores reflect cross-encoder, not original bi-encoder
    assert out[0].score == 0.9
    assert out[1].score == 0.5
    assert out[2].score == 0.1


def test_truncates_to_output_top_k() -> None:
    hits = [_hit(f"f{i}", f"text {i}", score=1.0 - i * 0.1) for i in range(5)]
    fake = FakeCrossEncoder({h.text: float(i) for i, h in enumerate(hits)})
    out = rerank_fact_hits(
        hits, "q",
        model=fake, rerank_top_k=10, output_top_k=2,
    )
    assert len(out) == 2
    # text 4 (score 4.0) and text 3 (score 3.0) are the top two
    assert [h.fact_id for h in out] == ["f4", "f3"]


def test_rerank_top_k_caps_rescored_items() -> None:
    hits = [_hit(f"f{i}", f"text {i}") for i in range(10)]
    fake = FakeCrossEncoder({h.text: 1.0 for h in hits})
    rerank_fact_hits(
        hits, "q",
        model=fake, rerank_top_k=3, output_top_k=10,
    )
    # Only the top-3 should have been sent to the cross-encoder
    assert len(fake.calls) == 1
    _, candidates = fake.calls[0]
    assert len(candidates) == 3
    assert candidates == ["text 0", "text 1", "text 2"]


def test_preserves_metadata_fields() -> None:
    hit = PageIndexFactHit(
        fact_id="f1",
        document_id="doc-1",
        line_num=42,
        text="user visited Dr. Patel on 2023-05-07",
        fact_type="experience",
        speaker="user",
        occurred_start=datetime(2023, 5, 7),
        occurred_end=None,
        entities=["Dr. Patel", "role:doctor"],
        score=0.5,
    )
    fake = FakeCrossEncoder({hit.text: 0.95})
    out = rerank_fact_hits([hit], "q", model=fake)
    assert len(out) == 1
    r = out[0]
    assert r.fact_id == "f1"
    assert r.document_id == "doc-1"
    assert r.line_num == 42
    assert r.fact_type == "experience"
    assert r.speaker == "user"
    assert r.occurred_start == datetime(2023, 5, 7)
    assert r.entities == ["Dr. Patel", "role:doctor"]
    assert r.score == 0.95


def test_protocol_compliance() -> None:
    """FakeCrossEncoder satisfies CrossEncoderProtocol structurally."""
    fake = FakeCrossEncoder({"a": 1.0})
    assert isinstance(fake, CrossEncoderProtocol)
