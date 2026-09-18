"""Rerank transforms must not silently drop ScoredItem fields.

The defect: ``basic_rerank`` and ``cross_encoder_like_rerank`` rebuilt
``ScoredItem`` field-by-field. They copied ``fact_type``, ``metadata``,
``tags``, ``memory_layer``, ``retained_at`` and ``chunk_id`` — and omitted
``occurred_at``. Both sit on the default recall path
(``final_rerank_mode="heuristic"``), so *every* recalled memory came back with
``occurred_at=None`` no matter what ``retain()`` was given.

It hid for two reasons. ``MemoryHit`` reads the field as
``getattr(item, "occurred_at", None)``, so a missing value is indistinguishable
from an absent one; and ``retained_at`` *was* copied, so hits still carried a
plausible-looking date — the ingest time. Downstream that produced memories
which all claimed to have happened on the day they were ingested.

These tests are deliberately generic over the field list rather than asserting
``occurred_at`` specifically: the bug is "a rebuild forgot a field", so any new
field added to ``ScoredItem`` is covered automatically. That is the actual
regression risk — the next field, not this one.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from astrocyte.pipeline.fusion import ScoredItem
from astrocyte.pipeline.reranking import (
    apply_context_diversity,
    basic_rerank,
    cross_encoder_like_rerank,
)

OCCURRED = datetime(2023, 7, 22, 4, 26, tzinfo=UTC)
RETAINED = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

# Every non-score field, populated with a distinguishable value. `score` is
# excluded because rerankers exist to change it.
FULLY_POPULATED = ScoredItem(
    id="m1",
    text="Alice bought a red kayak at the harbour store.",
    score=0.5,
    fact_type="episodic",
    metadata={"speaker": "alice", "session_id": "s1"},
    tags=["boats", "purchases"],
    memory_layer="raw",
    occurred_at=OCCURRED,
    retained_at=RETAINED,
    chunk_id="chunk-7",
)

PRESERVED_FIELDS = [f.name for f in dataclasses.fields(ScoredItem) if f.name != "score"]

TRANSFORMS = {
    "basic_rerank": lambda items: basic_rerank(items, "red kayak harbour"),
    "cross_encoder_like_rerank": lambda items: cross_encoder_like_rerank(
        items, "red kayak harbour"
    ),
    "apply_context_diversity": lambda items: apply_context_diversity(
        items, "red kayak harbour"
    ),
}


@pytest.mark.parametrize("name", sorted(TRANSFORMS))
@pytest.mark.parametrize("field", PRESERVED_FIELDS)
def test_transform_preserves_every_field(name, field):
    """Generic over fields: a future field is covered without editing this test."""
    out = TRANSFORMS[name]([FULLY_POPULATED])
    assert out, f"{name} returned no items"
    expected = getattr(FULLY_POPULATED, field)
    actual = getattr(out[0], field)
    assert actual == expected, (
        f"{name} dropped/altered {field!r}: {actual!r} != {expected!r}"
    )


class TestTheSpecificRegression:
    """The exact failure that shipped, pinned separately from the generic guard."""

    def test_occurred_at_survives_the_default_recall_rerank_chain(self):
        items = [FULLY_POPULATED]
        for fn in (basic_rerank, cross_encoder_like_rerank):
            items = fn(items, "red kayak harbour")
        items = apply_context_diversity(items, "red kayak harbour")
        assert items[0].occurred_at == OCCURRED

    def test_occurred_at_is_not_merely_equal_to_retained_at(self):
        """Guards the disguise: retained_at was copied, so hits looked dated."""
        out = basic_rerank([FULLY_POPULATED], "red kayak harbour")
        assert out[0].occurred_at != out[0].retained_at
        assert out[0].occurred_at == OCCURRED
