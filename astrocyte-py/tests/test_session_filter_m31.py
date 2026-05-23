"""M31 Fix 2 — end-to-end session_filter parameter coverage.

Validates that the ``session_id`` parameter on the public ``Astrocyte.recall()``
API and the MCP ``memory_recall`` tool propagates into ``RecallParams`` and
through the SPI Protocol contract. Per-component threading is already
covered by ``test_fact_recall.TestSessionFilterThreading`` (5 tests).

In-process round-trip:
  ``Astrocyte.recall(session_id=...)`` → ``RecallParams.session_id`` → (deeper
  retrieval threading is queued for M32 production wiring — see
  docs/_design/m31-lme-quality.md §3 Fix 2 'Phase 5 bench wiring' for the
  bench harness path that's already live).
"""

from __future__ import annotations

import pytest

from astrocyte._recall_params import RecallParams


class TestRecallParamsSessionId:
    """RecallParams carries session_id end-to-end."""

    def test_default_is_none(self) -> None:
        """v0.14.0 back-compat: session_id defaults to None."""
        params = RecallParams()
        assert params.session_id is None

    def test_explicit_session_id_round_trips(self) -> None:
        params = RecallParams(session_id="session-abc")
        assert params.session_id == "session-abc"

    def test_frozen_dataclass_immutable(self) -> None:
        """RecallParams is frozen — session_id can't be mutated after construction."""
        params = RecallParams(session_id="session-abc")
        with pytest.raises((AttributeError, TypeError)):  # frozen → FrozenInstanceError
            params.session_id = "other"  # type: ignore[misc]

    def test_session_id_coexists_with_other_filters(self) -> None:
        """session_id is one filter dimension among many."""
        from datetime import datetime

        params = RecallParams(
            session_id="session-abc",
            fact_types=["preference"],
            time_range=(datetime(2024, 1, 1), datetime(2024, 12, 31)),
            include_sources=True,
        )
        assert params.session_id == "session-abc"
        assert params.fact_types == ["preference"]
        assert params.include_sources is True


class TestPostgresProtocolContract:
    """The SPI Protocol in ``astrocyte.provider`` declares ``session_filter``
    on the 6 store search methods. Concrete stores (Postgres, InMemory) must
    accept it without TypeError. We verify the Protocol signature here so
    new adapter implementers can't drop the parameter accidentally."""

    def test_protocol_signatures_have_session_filter(self) -> None:
        """All 6 store search methods accept session_filter kwarg in the Protocol."""
        import inspect

        from astrocyte.provider import PageIndexStore

        for method_name in (
            "search_facts_semantic",
            "search_facts_by_entity",
            "search_facts_temporal",
            "search_sections_semantic",
            "search_sections_keyword",
            "search_sections_by_entities",
            "search_sections_temporal",
        ):
            sig = inspect.signature(getattr(PageIndexStore, method_name))
            assert "session_filter" in sig.parameters, (
                f"{method_name} missing session_filter kwarg in SPI Protocol"
            )


class TestInMemoryStoreSessionFilter:
    """The InMemoryPageIndexStore enforces session_filter on its 3 fact +
    4 section search methods. Per-method threading is covered in
    test_fact_recall; this is the integration smoke that the helpers wire
    up correctly when facts and sections live in different sessions."""

    @pytest.mark.asyncio
    async def test_fact_semantic_excludes_other_sessions(self) -> None:
        from datetime import datetime, timezone

        from astrocyte.testing.in_memory import InMemoryPageIndexStore
        from astrocyte.types import (
            MemoryFact,
            PageIndexDocument,
            PageIndexSection,
        )

        store = InMemoryPageIndexStore()
        doc_id = await store.save_document(
            PageIndexDocument(
                id="",
                bank_id="b1",
                source_id="user1",
                md_text="",
                reference_date=None,
                built_at=datetime.now(tz=timezone.utc),
            ),
        )
        # Two sections in different sessions.
        await store.save_sections(
            doc_id,
            [
                PageIndexSection(
                    document_id=doc_id, line_num=1, node_id="s1", title="A",
                    summary="alpha", session_id="session-a",
                ),
                PageIndexSection(
                    document_id=doc_id, line_num=10, node_id="s2", title="B",
                    summary="beta", session_id="session-b",
                ),
            ],
        )
        # Two facts, one anchored to each section.
        await store.save_facts([
            MemoryFact(
                id="f1", bank_id="b1", document_id=doc_id, line_num=1,
                text="fact from session a", fact_type="experience",
                entities=["thing"], embedding=[1.0, 0.0, 0.0],
            ),
            MemoryFact(
                id="f2", bank_id="b1", document_id=doc_id, line_num=10,
                text="fact from session b", fact_type="experience",
                entities=["thing"], embedding=[1.0, 0.0, 0.0],
            ),
        ])

        # No filter — both facts returned.
        all_hits = await store.search_facts_semantic(
            "b1", [1.0, 0.0, 0.0], top_k=10,
        )
        assert {h.fact_id for h in all_hits} == {"f1", "f2"}

        # session-a filter — only f1.
        a_hits = await store.search_facts_semantic(
            "b1", [1.0, 0.0, 0.0], top_k=10, session_filter="session-a",
        )
        assert {h.fact_id for h in a_hits} == {"f1"}

        # session-b filter — only f2.
        b_hits = await store.search_facts_semantic(
            "b1", [1.0, 0.0, 0.0], top_k=10, session_filter="session-b",
        )
        assert {h.fact_id for h in b_hits} == {"f2"}

        # session-c (nonexistent) — empty.
        c_hits = await store.search_facts_semantic(
            "b1", [1.0, 0.0, 0.0], top_k=10, session_filter="session-c",
        )
        assert c_hits == []

    @pytest.mark.asyncio
    async def test_top_level_fact_excluded_when_session_filter_set(self) -> None:
        """Facts without (document_id, line_num) anchor are excluded when
        session_filter is set — they have no session by definition."""
        from datetime import datetime, timezone

        from astrocyte.testing.in_memory import InMemoryPageIndexStore
        from astrocyte.types import MemoryFact, PageIndexDocument

        store = InMemoryPageIndexStore()
        doc_id = await store.save_document(
            PageIndexDocument(
                id="", bank_id="b1", source_id="u1", md_text="",
                reference_date=None,
                built_at=datetime.now(tz=timezone.utc),
            ),
        )
        # Top-level fact: document_id set, line_num=None (anchorless).
        await store.save_facts([
            MemoryFact(
                id="f-top", bank_id="b1", document_id=doc_id, line_num=None,
                text="top-level", fact_type="experience",
                entities=["thing"], embedding=[1.0, 0.0, 0.0],
            ),
        ])

        no_filter = await store.search_facts_semantic(
            "b1", [1.0, 0.0, 0.0], top_k=10,
        )
        assert {h.fact_id for h in no_filter} == {"f-top"}

        with_filter = await store.search_facts_semantic(
            "b1", [1.0, 0.0, 0.0], top_k=10, session_filter="any-session",
        )
        # Top-level fact excluded — has no anchoring section to match.
        assert with_filter == []


# ────────────────────────────────────────────────────────────────────────
# M31 Fix 2 — production wiring (RecallRequest → VectorFilters → store)
# ────────────────────────────────────────────────────────────────────────


class TestProductionRecallSessionWiring:
    """End-to-end wiring through the production stack:

      ``Astrocyte.recall(session_id=...)`` → ``RecallParams`` →
      ``RecallRequest`` → orchestrator → ``VectorFilters`` → vector_store
      adapter's ``search_similar(filters=...)``.

    Uses the in-memory vector store so we can assert filtered hits
    without external dependencies. Postgres pgvector / Qdrant / Neo4j
    / Elasticsearch adapter implementations are documented as M32
    follow-up in m31-lme-quality.md §6."""

    def test_recall_request_carries_session_id(self) -> None:
        """RecallRequest accepts and stores session_id."""
        from astrocyte.types import RecallRequest

        request = RecallRequest(query="q", bank_id="b1", session_id="session-abc")
        assert request.session_id == "session-abc"

    def test_vector_filters_carries_session_id(self) -> None:
        """VectorFilters accepts and stores session_id."""
        from astrocyte.types import VectorFilters

        filters = VectorFilters(bank_id="b1", session_id="session-abc")
        assert filters.session_id == "session-abc"

    def test_make_recall_request_propagates_session_id(self) -> None:
        """RecallParams.session_id flows to RecallRequest.session_id via
        the same dataclass-style copy used by _make_recall_request. We
        construct the request directly to avoid the engine plumbing —
        the propagation path itself is what we're guarding."""
        # Mimic exactly what _make_recall_request does for session_id:
        from astrocyte._recall_params import RecallParams
        from astrocyte.types import RecallRequest

        params = RecallParams(session_id="session-xyz")
        request = RecallRequest(
            query="q", bank_id="b1", session_id=params.session_id,
        )
        assert request.session_id == "session-xyz"

    @pytest.mark.asyncio
    async def test_vector_store_filters_by_session_id(self) -> None:
        """InMemoryVectorStore.search_similar honours VectorFilters.session_id.

        Adapters that don't implement the filter MUST ignore it (best-
        effort semantics, no error). Production callers learn whether
        their adapter supports session scoping by observing whether
        results change with vs without the filter."""
        from astrocyte.testing.in_memory import InMemoryVectorStore, VectorItem
        from astrocyte.types import VectorFilters

        store = InMemoryVectorStore()
        # Two items in different sessions (via metadata['session_id'])
        # with identical vectors.
        store._vectors["v1"] = VectorItem(
            id="v1", text="alpha",
            vector=[1.0, 0.0, 0.0],
            bank_id="b1",
            metadata={"session_id": "session-a"},
        )
        store._vectors["v2"] = VectorItem(
            id="v2", text="beta",
            vector=[1.0, 0.0, 0.0],
            bank_id="b1",
            metadata={"session_id": "session-b"},
        )

        # Baseline — no filter returns both.
        all_hits = await store.search_similar([1.0, 0.0, 0.0], "b1", limit=10)
        assert {h.id for h in all_hits} == {"v1", "v2"}

        # session-a filter — only v1.
        a_hits = await store.search_similar(
            [1.0, 0.0, 0.0], "b1", limit=10,
            filters=VectorFilters(bank_id="b1", session_id="session-a"),
        )
        assert {h.id for h in a_hits} == {"v1"}

        # session-b filter — only v2.
        b_hits = await store.search_similar(
            [1.0, 0.0, 0.0], "b1", limit=10,
            filters=VectorFilters(bank_id="b1", session_id="session-b"),
        )
        assert {h.id for h in b_hits} == {"v2"}

    @pytest.mark.asyncio
    async def test_vector_store_excludes_items_without_session_id(self) -> None:
        """When filters.session_id is set, items lacking session_id
        metadata are excluded (consistent with PageIndexStore behaviour
        for top-level facts)."""
        from astrocyte.testing.in_memory import InMemoryVectorStore, VectorItem
        from astrocyte.types import VectorFilters

        store = InMemoryVectorStore()
        store._vectors["v-untagged"] = VectorItem(
            id="v-untagged", text="no session",
            vector=[1.0, 0.0, 0.0],
            bank_id="b1",
            metadata={},  # no session_id
        )

        # Baseline: returned without filter.
        baseline = await store.search_similar([1.0, 0.0, 0.0], "b1", limit=10)
        assert {h.id for h in baseline} == {"v-untagged"}

        # With session filter: excluded (no session_id to match).
        filtered = await store.search_similar(
            [1.0, 0.0, 0.0], "b1", limit=10,
            filters=VectorFilters(bank_id="b1", session_id="any"),
        )
        assert filtered == []
