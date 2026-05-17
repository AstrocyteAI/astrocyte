"""Tests for the ``MemoryFact`` rename + nullable section anchor.

Covers the M18b post-ship cleanup (2026-05-17):
  - ``MemoryFact`` is the canonical name; ``PageIndexFact`` remains as
    a backward-compat alias resolving to the same dataclass.
  - ``MemoryFactHit`` / ``PageIndexFactHit`` alias the same way.
  - ``document_id`` and ``line_num`` are optional (None by default);
    Hindsight-parity top-level facts can be constructed with no
    section anchor at all.
  - In-memory store accepts and round-trips top-level facts.

See `astrocyte/types.py` for the canonical definitions and
`adapters-storage-py/.../migrations/029_pi_facts_nullable_document_id.sql`
for the Postgres-side change.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from astrocyte.testing.in_memory import InMemoryPageIndexStore
from astrocyte.types import (
    MemoryFact,
    MemoryFactHit,
    PageIndexFact,
    PageIndexFactHit,
)


class TestBackwardCompatAlias:
    def test_pageindexfact_is_memoryfact(self) -> None:
        """The deprecated alias must resolve to the same dataclass."""
        assert PageIndexFact is MemoryFact

    def test_pageindexfacthit_is_memoryfacthit(self) -> None:
        assert PageIndexFactHit is MemoryFactHit

    def test_isinstance_check_works_through_alias(self) -> None:
        """A fact constructed via the new name must satisfy isinstance(x, PageIndexFact)."""
        f = MemoryFact(
            id="f1",
            bank_id="b1",
            document_id="d1",
            line_num=5,
            text="hello",
            fact_type="experience",
        )
        assert isinstance(f, PageIndexFact)
        assert isinstance(f, MemoryFact)


class TestAnchoredFactStillWorks:
    """The pre-M18b shape — section-anchored fact — must keep working."""

    def test_construct_with_kwargs(self) -> None:
        f = MemoryFact(
            id="f1",
            bank_id="b1",
            document_id="d1",
            line_num=5,
            text="User visited Paris",
            fact_type="experience",
            speaker="user",
            occurred_start=datetime(2024, 5, 7),
            entities=["Paris"],
        )
        assert f.document_id == "d1"
        assert f.line_num == 5
        assert f.text == "User visited Paris"


class TestTopLevelFactNoAnchor:
    """Hindsight-parity: a fact can exist without a section anchor."""

    def test_construct_with_no_document_id(self) -> None:
        """Both document_id and line_num default to None."""
        f = MemoryFact(
            id="f1",
            bank_id="b1",
            text="The sky is blue",
            fact_type="world",
        )
        assert f.document_id is None
        assert f.line_num is None

    def test_construct_with_explicit_none(self) -> None:
        f = MemoryFact(
            id="f1",
            bank_id="b1",
            text="The user prefers vegan",
            fact_type="preference",
            document_id=None,
            line_num=None,
        )
        assert f.document_id is None
        assert f.line_num is None

    @pytest.mark.asyncio
    async def test_in_memory_store_round_trips_top_level_fact(self) -> None:
        """Top-level facts (no anchor) must save + appear in semantic search.

        This verifies the in-memory store doesn't crash on `None`
        document_id (the path Postgres takes is covered by migration 029
        + executemany passing through None as SQL NULL).
        """
        store = InMemoryPageIndexStore()
        top_level = MemoryFact(
            id="f1",
            bank_id="b1",
            text="The user lives in Tokyo",
            fact_type="experience",
            embedding=[0.1, 0.2, 0.3],
        )
        anchored = MemoryFact(
            id="f2",
            bank_id="b1",
            document_id="doc-1",
            line_num=3,
            text="User mentioned a recent trip",
            fact_type="experience",
            embedding=[0.1, 0.2, 0.3],
        )
        n = await store.save_facts([top_level, anchored])
        assert n == 2

        # No document_id filter → both facts returned (semantic search)
        hits = await store.search_facts_semantic(
            "b1", [0.1, 0.2, 0.3], top_k=10, document_id=None,
        )
        hit_ids = {getattr(h, "fact_id", None) for h in hits}
        assert "f1" in hit_ids
        assert "f2" in hit_ids


class TestMemoryFactHitNullableAnchor:
    def test_hit_with_no_anchor(self) -> None:
        h = MemoryFactHit(
            fact_id="f1",
            text="hello",
            fact_type="experience",
            speaker=None,
            occurred_start=None,
            occurred_end=None,
            entities=[],
            score=0.9,
        )
        assert h.document_id is None
        assert h.line_num is None
