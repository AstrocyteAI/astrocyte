"""Integration tests for ``PostgresStore.search_fulltext_bm25``.

Hits a live PostgreSQL with migrations 001-013 applied (skipped if
``DATABASE_URL`` unset). Each test seeds its own bank to avoid
cross-test pollution.

The test corpus is intentionally small so we can predict the IDF
distribution: distinctive proper nouns ("Zaragoza") have very high IDF,
common stopword-ish terms ("the") have ≈ 0 IDF. The asserts compare
ratios rather than absolute scores because absolute scores depend on
corpus size + ts_rank_cd internals.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from astrocyte.types import VectorItem

from astrocyte_postgres.store import PostgresStore


@pytest.fixture
def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — skipping BM25 IDF integration tests")
    return url


@pytest.fixture
async def seeded_store(dsn: str):
    """Build a store, seed a small bank with predictable corpus, refresh
    the BM25 views once, then yield."""
    bank = f"bm25-bank-{uuid.uuid4().hex[:8]}"
    store = PostgresStore(dsn=dsn, bootstrap_schema=False, embedding_dimensions=1536)

    # Use a constant vector to dodge embedding generation — semantic
    # ranking isn't what we're testing here.
    zero = [0.0] * 1536

    # Corpus design (10 docs in this bank):
    #   - 3 docs talk about "Zaragoza" (rare proper noun → high IDF)
    #   - 5 docs talk about "Calvin" (common across corpus)
    #   - 2 docs talk about "the meeting" (only common stopword-ish words)
    seeds = [
        ("z1", "Zaragoza is a beautiful city"),
        ("z2", "Travelled to Zaragoza last summer"),
        ("z3", "Zaragoza has great food"),
        ("c1", "Calvin started a new job"),
        ("c2", "Calvin moved apartments"),
        ("c3", "Calvin learned Spanish"),
        ("c4", "Calvin and the team won"),
        ("c5", "Calvin loves coffee"),
        ("m1", "the meeting was on Monday"),
        ("m2", "the meeting ran long"),
    ]
    items = [
        VectorItem(
            id=f"{bank}::{mid}",
            bank_id=bank,
            vector=zero,
            text=text,
            metadata={},
            occurred_at=datetime.now(UTC),
        )
        for mid, text in seeds
    ]
    await store.store_vectors(items)
    # Refresh the BM25 / IDF materialized views so the new docs are visible.
    await store.refresh_bm25_views(concurrent=False)

    yield store, bank

    # Teardown: hard-delete the bank's rows so the next test sees a clean slate.
    conn = await psycopg.AsyncConnection.connect(dsn)
    async with conn:
        await conn.execute(
            "DELETE FROM public.astrocyte_vectors WHERE bank_id = %s", [bank],
        )
        await conn.commit()


class TestBm25IdfQuery:
    """End-to-end behaviour of ``search_fulltext_bm25``."""

    @pytest.mark.asyncio
    async def test_returns_hits_for_existing_terms(self, seeded_store):
        store, bank = seeded_store
        hits = await store.search_fulltext_bm25("Zaragoza", bank, limit=10)
        ids = {h.document_id.split("::", 1)[1] for h in hits}
        assert ids == {"z1", "z2", "z3"}

    @pytest.mark.asyncio
    async def test_empty_query_returns_no_hits(self, seeded_store):
        store, bank = seeded_store
        assert await store.search_fulltext_bm25("", bank, limit=10) == []
        assert await store.search_fulltext_bm25("   ", bank, limit=10) == []

    @pytest.mark.asyncio
    async def test_unknown_terms_return_no_hits(self, seeded_store):
        store, bank = seeded_store
        assert await store.search_fulltext_bm25(
            "termthatdoesntexist", bank, limit=10,
        ) == []

    @pytest.mark.asyncio
    async def test_bm25_score_is_higher_for_rare_term_query(self, seeded_store):
        """The whole point: queries with rare terms must score HIGHER than
        queries with common terms via IDF weighting. This is the metric
        the bench gain depends on."""
        store, bank = seeded_store

        # "Zaragoza" appears in 3/10 docs — high IDF
        rare_hits = await store.search_fulltext_bm25("Zaragoza", bank, limit=5)
        # "the" appears in 2/10 docs but is a Postgres stop word, so it
        # gets dropped from the tsvector entirely. Use "Calvin" instead —
        # appears in 5/10 docs, common but real lexeme.
        common_hits = await store.search_fulltext_bm25("Calvin", bank, limit=5)

        assert rare_hits and common_hits
        # The TOP-1 score for the rare-term query must exceed the top-1
        # for the common-term query, all else equal.
        assert rare_hits[0].score > common_hits[0].score, (
            f"BM25 IDF should boost rare-term queries: "
            f"Zaragoza top={rare_hits[0].score:.4f}, "
            f"Calvin top={common_hits[0].score:.4f}"
        )

    @pytest.mark.asyncio
    async def test_results_carry_text_and_metadata(self, seeded_store):
        """The query has to fetch text + metadata via a follow-up join
        (the BM25 MV doesn't carry them). Verify the round-trip."""
        store, bank = seeded_store
        hits = await store.search_fulltext_bm25("Zaragoza", bank, limit=10)
        for h in hits:
            assert h.text  # non-empty
            assert isinstance(h.metadata, dict) or h.metadata is None
            assert h.document_id.startswith(bank)

    @pytest.mark.asyncio
    async def test_results_ordered_by_score_desc(self, seeded_store):
        store, bank = seeded_store
        hits = await store.search_fulltext_bm25("Calvin", bank, limit=10)
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True), (
            f"hits must be ordered by score DESC; got {scores}"
        )

    @pytest.mark.asyncio
    async def test_limit_caps_returned_count(self, seeded_store):
        store, bank = seeded_store
        # 5 Calvin-docs in the corpus; ask for 2.
        hits = await store.search_fulltext_bm25("Calvin", bank, limit=2)
        assert len(hits) <= 2


class TestRefreshBm25Views:
    """``refresh_bm25_views`` must keep the views consistent with the
    underlying ``astrocyte_vectors`` table."""

    @pytest.mark.asyncio
    async def test_new_memories_invisible_until_refresh(self, dsn: str):
        bank = f"refresh-bank-{uuid.uuid4().hex[:8]}"
        store = PostgresStore(dsn=dsn, bootstrap_schema=False, embedding_dimensions=1536)
        zero = [0.0] * 1536

        try:
            # Seed + refresh — baseline visible.
            await store.store_vectors([
                VectorItem(
                    id=f"{bank}::initial", bank_id=bank, vector=zero,
                    text="Klingon Bird-of-Prey", metadata={},
                ),
            ])
            await store.refresh_bm25_views(concurrent=False)
            initial = await store.search_fulltext_bm25("Klingon", bank, limit=10)
            assert len(initial) == 1

            # Add a new memory but DON'T refresh — should NOT show up yet.
            await store.store_vectors([
                VectorItem(
                    id=f"{bank}::new", bank_id=bank, vector=zero,
                    text="Romulan Warbird is also Klingon-adjacent", metadata={},
                ),
            ])
            stale = await store.search_fulltext_bm25("Klingon", bank, limit=10)
            assert len(stale) == 1, (
                "BM25 view is materialized — new memory must NOT appear "
                "until refresh_bm25_views is called"
            )

            # Refresh — now the new memory IS visible.
            await store.refresh_bm25_views(concurrent=False)
            after = await store.search_fulltext_bm25("Klingon", bank, limit=10)
            assert len(after) == 2
        finally:
            conn = await psycopg.AsyncConnection.connect(dsn)
            async with conn:
                await conn.execute(
                    "DELETE FROM public.astrocyte_vectors WHERE bank_id = %s", [bank],
                )
                await conn.commit()
