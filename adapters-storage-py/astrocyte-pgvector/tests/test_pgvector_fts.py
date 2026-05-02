"""Integration tests for PgVectorStore as DocumentStore (pg_tsvector FTS).

These tests verify that PgVectorStore satisfies the DocumentStore protocol:
- search_fulltext returns ranked results via ts_rank on the text_fts column
- store_document is a no-op (data already stored by store_vectors)
- get_document retrieves memories as Document objects
- forgotten memories are excluded from FTS results
- the keyword strategy activates in parallel_retrieve when the store is wired

All tests require DATABASE_URL and are skipped when it's not set.
"""

from __future__ import annotations

from astrocyte.types import Document

from astrocyte_pgvector.store import PgVectorStore
from tests.conftest import DIM, make_item

# ---------------------------------------------------------------------------
# search_fulltext
# ---------------------------------------------------------------------------


class TestSearchFulltext:
    async def test_returns_matching_memories(self, store: PgVectorStore) -> None:
        """Memories whose text matches the query are returned."""
        await store.store_vectors([
            make_item("m1", text="Alice went to the coffee shop on Tuesday"),
            make_item("m2", text="Bob prefers tea over coffee"),
            make_item("m3", text="The weather was sunny all week"),
        ])

        hits = await store.search_fulltext("coffee", "bank-1", limit=10)

        ids = {h.document_id for h in hits}
        assert "m1" in ids
        assert "m2" in ids
        assert "m3" not in ids

    async def test_returns_empty_for_no_match(self, store: PgVectorStore) -> None:
        await store.store_vectors([
            make_item("m1", text="Alice loves hiking in the mountains"),
        ])

        hits = await store.search_fulltext("quantum computing", "bank-1", limit=10)
        assert hits == []

    async def test_results_ordered_by_relevance(self, store: PgVectorStore) -> None:
        """Memory with more query-term occurrences ranks higher."""
        await store.store_vectors([
            make_item("low",  text="coffee mentioned once"),
            make_item("high", text="coffee coffee coffee is Alice's favourite morning coffee"),
        ])

        hits = await store.search_fulltext("coffee", "bank-1", limit=10)
        assert len(hits) == 2
        assert hits[0].document_id == "high"

    async def test_respects_bank_isolation(self, store: PgVectorStore) -> None:
        """FTS does not cross bank boundaries."""
        await store.store_vectors([
            make_item("m1", bank_id="bank-a", text="unique term xyzzy appears here"),
            make_item("m2", bank_id="bank-b", text="unique term xyzzy in another bank"),
        ])

        hits_a = await store.search_fulltext("xyzzy", "bank-a", limit=10)
        hits_b = await store.search_fulltext("xyzzy", "bank-b", limit=10)

        assert {h.document_id for h in hits_a} == {"m1"}
        assert {h.document_id for h in hits_b} == {"m2"}

    async def test_forgotten_memories_excluded(self, store: PgVectorStore) -> None:
        """Soft-deleted memories must not appear in FTS results."""
        await store.store_vectors([
            make_item("alive", text="coffee is great"),
            make_item("dead",  text="coffee is terrible"),
        ])
        await store.delete(["dead"], "bank-1")

        hits = await store.search_fulltext("coffee", "bank-1", limit=10)
        ids = {h.document_id for h in hits}
        assert "alive" in ids
        assert "dead" not in ids

    async def test_empty_query_returns_empty(self, store: PgVectorStore) -> None:
        await store.store_vectors([make_item("m1", text="some content")])
        assert await store.search_fulltext("", "bank-1") == []
        assert await store.search_fulltext("   ", "bank-1") == []

    async def test_hit_shape(self, store: PgVectorStore) -> None:
        """Each DocumentHit has the required fields."""
        await store.store_vectors([
            make_item("m1", text="Alice attended the conference", metadata={"source": "chat"}),
        ])

        hits = await store.search_fulltext("conference", "bank-1", limit=5)
        assert len(hits) == 1
        h = hits[0]
        assert h.document_id == "m1"
        assert "conference" in h.text
        assert isinstance(h.score, float) and h.score > 0
        assert h.metadata == {"source": "chat"}

    async def test_tags_filter(self, store: PgVectorStore) -> None:
        """DocumentFilters.tags restricts FTS to tagged memories."""
        from astrocyte.types import DocumentFilters

        await store.store_vectors([
            make_item("tagged",   text="coffee shop visit", tags=["diary"]),
            make_item("untagged", text="coffee shop visit"),
        ])

        hits = await store.search_fulltext(
            "coffee", "bank-1", limit=10,
            filters=DocumentFilters(tags=["diary"]),
        )
        ids = {h.document_id for h in hits}
        assert "tagged" in ids
        assert "untagged" not in ids


# ---------------------------------------------------------------------------
# store_document  (no-op contract)
# ---------------------------------------------------------------------------


class TestStoreDocument:
    async def test_returns_document_id(self, store: PgVectorStore) -> None:
        doc = Document(id="doc-1", text="hello world")
        result = await store.store_document(doc, "bank-1")
        assert result == "doc-1"

    async def test_idempotent_noop(self, store: PgVectorStore) -> None:
        """Calling store_document multiple times must not raise."""
        doc = Document(id="doc-2", text="repeated content")
        await store.store_document(doc, "bank-1")
        await store.store_document(doc, "bank-1")  # second call must not fail


# ---------------------------------------------------------------------------
# get_document
# ---------------------------------------------------------------------------


class TestGetDocument:
    async def test_returns_none_for_missing(self, store: PgVectorStore) -> None:
        result = await store.get_document("nonexistent", "bank-1")
        assert result is None

    async def test_returns_document_for_stored_memory(self, store: PgVectorStore) -> None:
        await store.store_vectors([
            make_item("m1", text="Alice's birthday party", metadata={"year": 2025}),
        ])

        doc = await store.get_document("m1", "bank-1")
        assert doc is not None
        assert doc.id == "m1"
        assert "birthday" in doc.text
        assert doc.metadata == {"year": 2025}

    async def test_bank_isolation(self, store: PgVectorStore) -> None:
        """get_document must not return memories from a different bank."""
        await store.store_vectors([make_item("m1", bank_id="bank-a", text="content")])

        assert await store.get_document("m1", "bank-b") is None
        assert await store.get_document("m1", "bank-a") is not None

    async def test_forgotten_memory_returns_none(self, store: PgVectorStore) -> None:
        await store.store_vectors([make_item("m1", text="to be forgotten")])
        await store.delete(["m1"], "bank-1")

        assert await store.get_document("m1", "bank-1") is None


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestDocumentStoreProtocol:
    def test_has_all_protocol_methods(self) -> None:
        """PgVectorStore must expose every method in the DocumentStore protocol."""
        for method in ("store_document", "search_fulltext", "get_document", "health"):
            assert hasattr(PgVectorStore, method), f"Missing DocumentStore method: {method}"

    async def test_fts_activates_keyword_strategy(self, store: PgVectorStore) -> None:
        """When PgVectorStore is passed as document_store, parallel_retrieve
        includes the keyword strategy in the fused result set."""
        from astrocyte.pipeline.retrieval import parallel_retrieve

        await store.store_vectors([
            make_item("m1", text="Alice loves jazz music"),
            make_item("m2", text="Bob is a classical pianist"),
        ])
        # Embed a dummy query vector (same dim as store)
        query_vector = [0.1] * DIM

        result = await parallel_retrieve(
            query_vector=query_vector,
            query_text="jazz music",
            bank_id="bank-1",
            vector_store=store,
            document_store=store,   # <-- PgVectorStore as DocumentStore
            graph_store=None,
            limit=10,
        )

        # keyword strategy should have contributed m1 (contains "jazz")
        ids = {r.id for r in result}
        assert "m1" in ids, (
            "parallel_retrieve with PgVectorStore as document_store must activate "
            "the keyword strategy and return the lexical match."
        )
