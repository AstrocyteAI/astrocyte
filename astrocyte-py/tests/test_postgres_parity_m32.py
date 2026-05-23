"""M32 — Postgres adapter parity tests for M31 + M32 changes.

Validates that the ``PostgresPageIndexStore`` adapter produces the
SAME observable behaviour as ``InMemoryPageIndexStore`` for the
M31/M32 surface area:

  - ``PageIndexSection.session_id`` round-trips (migration 035)
  - ``MemoryFact.event_date`` round-trips (migration 036)
  - ``search_facts_semantic(session_filter=...)`` filters correctly
  - ``search_facts_by_entity(session_filter=...)`` filters correctly
  - ``PageIndexPipeline.recall(session_id=...)`` works on Postgres
  - All 3 fact loaders hydrate ``event_date`` from the DB row

Skip-gated on ``ASTROCYTE_PG_DSN`` so unit-test runs (no DB) skip
silently. The bench DBs (``BENCH_DATABASE_URL`` / ``_2``) work too;
set ``ASTROCYTE_PG_DSN`` to either of them or to a dedicated test DB.

These tests catch the class of bug we hit in M30-R2 (bench-runner
state pollution) and migrations-don't-match-bootstrap (M28-A): the
in-process adapter must match the migration-applied schema.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

_PG_DSN = os.environ.get("ASTROCYTE_PG_DSN")

# Postgres pgvector column is fixed at 1536 dims (OpenAI ada-002 size).
# Synthesize a 1536-d "all weight on dimension 0" vector so cosine
# distance against a similarly-shaped query is well-defined.
_EMB_DIM = 1536


def _vec_dim0() -> list[float]:
    """1536-d vector with 1.0 in slot 0, zeros elsewhere."""
    v = [0.0] * _EMB_DIM
    v[0] = 1.0
    return v


pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="Set ASTROCYTE_PG_DSN to run M32 Postgres parity tests",
)


@pytest.fixture
async def pg_store():
    """A PostgresPageIndexStore connected to a clean test schema.

    Uses a per-test schema name so concurrent test runs don't collide.
    The schema is dropped at teardown.
    """
    import psycopg
    from astrocyte_postgres.pageindex_store import PostgresPageIndexStore

    schema = f"m32_parity_{os.getpid()}"
    # Wipe + recreate so each test starts clean.
    async with await psycopg.AsyncConnection.connect(_PG_DSN, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await cur.execute(f"CREATE SCHEMA {schema}")
    store = PostgresPageIndexStore(
        dsn=_PG_DSN,
        schema=schema,
        bootstrap_schema=True,
    )
    yield store
    # Teardown — drop the schema.
    async with await psycopg.AsyncConnection.connect(_PG_DSN, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


async def _seed_two_sessions(store):
    """Helper — two facts in two sessions, same bank/document.

    Returns (document_id, fact_a_id, fact_b_id). Fact IDs are UUIDs
    because the Postgres adapter requires UUID type. The in-memory
    store accepts arbitrary strings; using UUID here keeps both
    adapters happy.
    """
    import uuid

    from astrocyte.types import MemoryFact, PageIndexDocument, PageIndexSection

    fact_a_id = str(uuid.uuid4())
    fact_b_id = str(uuid.uuid4())

    doc_id = await store.save_document(
        PageIndexDocument(
            id="", bank_id="b1", source_id="u1", md_text="",
            reference_date=None,
            built_at=datetime.now(tz=timezone.utc),
        ),
    )
    await store.save_sections(doc_id, [
        PageIndexSection(
            document_id=doc_id, line_num=1, node_id="s1",
            title="Session A", summary="alpha session",
            session_id="session-A",
            session_date=datetime(2024, 5, 7, tzinfo=timezone.utc),
        ),
        PageIndexSection(
            document_id=doc_id, line_num=10, node_id="s2",
            title="Session B", summary="beta session",
            session_id="session-B",
            session_date=datetime(2024, 5, 9, tzinfo=timezone.utc),
        ),
    ])
    await store.save_facts([
        MemoryFact(
            id=fact_a_id, bank_id="b1", document_id=doc_id, line_num=1,
            text="Alpha fact", fact_type="experience",
            entities=["thing", "alpha-entity"],
            embedding=_vec_dim0(),
            confidence_score=0.9,
            mentioned_at=datetime(2024, 5, 7, tzinfo=timezone.utc),
            event_date=datetime(2024, 5, 5, tzinfo=timezone.utc),
        ),
        MemoryFact(
            id=fact_b_id, bank_id="b1", document_id=doc_id, line_num=10,
            text="Beta fact", fact_type="experience",
            entities=["thing", "beta-entity"],
            embedding=_vec_dim0(),
            confidence_score=0.7,
            mentioned_at=datetime(2024, 5, 9, tzinfo=timezone.utc),
            event_date=None,  # legacy / no parseable phrase
        ),
    ])
    return doc_id, fact_a_id, fact_b_id


class TestPostgresSessionIdRoundTrip:
    @pytest.mark.asyncio
    async def test_section_session_id_roundtrips(self, pg_store) -> None:
        """Migration 035 column + bootstrap mirror — session_id stored, retrieved."""
        doc_id, _, _ = await _seed_two_sessions(pg_store)
        sections = await pg_store.load_skeleton(doc_id)
        by_line = {s.line_num: s for s in sections}
        assert by_line[1].session_id == "session-A"
        assert by_line[10].session_id == "session-B"


class TestPostgresEventDateRoundTrip:
    @pytest.mark.asyncio
    async def test_event_date_stored_and_loaded(self, pg_store) -> None:
        """Migration 036 column + bootstrap mirror — event_date end-to-end."""
        _, fa_id, fb_id = await _seed_two_sessions(pg_store)
        hits = await pg_store.search_facts_semantic(
            "b1", _vec_dim0(), top_k=10,
        )
        by_id = {h.fact_id: h for h in hits}
        assert by_id[fa_id].event_date == datetime(2024, 5, 5, tzinfo=timezone.utc)
        assert by_id[fb_id].event_date is None

    @pytest.mark.asyncio
    async def test_event_date_via_by_entity_loader(self, pg_store) -> None:
        _, fa_id, _ = await _seed_two_sessions(pg_store)
        hits = await pg_store.search_facts_by_entity(
            "b1", "alpha-entity", top_k=10,
        )
        assert len(hits) == 1
        assert hits[0].fact_id == fa_id
        assert hits[0].event_date == datetime(2024, 5, 5, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_event_date_via_temporal_loader(self, pg_store) -> None:
        import uuid

        from astrocyte.types import MemoryFact

        doc_id, _, _ = await _seed_two_sessions(pg_store)
        ft_id = str(uuid.uuid4())
        await pg_store.save_facts([
            MemoryFact(
                id=ft_id, bank_id="b1", document_id=doc_id, line_num=10,
                text="Temporal fact", fact_type="experience",
                entities=["t"], embedding=_vec_dim0(),
                occurred_start=datetime(2024, 6, 15, tzinfo=timezone.utc),
                event_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
            ),
        ])
        hits = await pg_store.search_facts_temporal(
            "b1",
            (
                datetime(2024, 6, 1, tzinfo=timezone.utc),
                datetime(2024, 6, 30, tzinfo=timezone.utc),
            ),
            top_k=10,
        )
        ft = [h for h in hits if h.fact_id == ft_id]
        assert len(ft) == 1
        assert ft[0].event_date == datetime(2024, 6, 1, tzinfo=timezone.utc)


class TestPostgresSessionFilter:
    @pytest.mark.asyncio
    async def test_semantic_session_filter(self, pg_store) -> None:
        _, fa_id, fb_id = await _seed_two_sessions(pg_store)
        all_hits = await pg_store.search_facts_semantic(
            "b1", _vec_dim0(), top_k=10,
        )
        assert {h.fact_id for h in all_hits} == {fa_id, fb_id}

        a_hits = await pg_store.search_facts_semantic(
            "b1", _vec_dim0(), top_k=10, session_filter="session-A",
        )
        assert {h.fact_id for h in a_hits} == {fa_id}

        b_hits = await pg_store.search_facts_semantic(
            "b1", _vec_dim0(), top_k=10, session_filter="session-B",
        )
        assert {h.fact_id for h in b_hits} == {fb_id}

    @pytest.mark.asyncio
    async def test_by_entity_session_filter(self, pg_store) -> None:
        _, fa_id, _ = await _seed_two_sessions(pg_store)
        a_hits = await pg_store.search_facts_by_entity(
            "b1", "thing", top_k=10, session_filter="session-A",
        )
        assert {h.fact_id for h in a_hits} == {fa_id}

    @pytest.mark.asyncio
    async def test_session_filter_nonexistent_returns_empty(self, pg_store) -> None:
        await _seed_two_sessions(pg_store)
        hits = await pg_store.search_facts_semantic(
            "b1", _vec_dim0(), top_k=10,
            session_filter="session-doesnt-exist",
        )
        assert hits == []


class TestPostgresPipelineEnd2End:
    @pytest.mark.asyncio
    async def test_pageindex_pipeline_recall_with_session_filter(self, pg_store) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from astrocyte.pipeline.pageindex_pipeline import PageIndexPipeline
        from astrocyte.types import RecallRequest

        _, fa_id, fb_id = await _seed_two_sessions(pg_store)
        provider = MagicMock()
        provider.embed = AsyncMock(return_value=[_vec_dim0()])
        pipeline = PageIndexPipeline(
            store=pg_store, embedding_provider=provider,
        )

        a_result = await pipeline.recall(
            RecallRequest(query="alpha", bank_id="b1", session_id="session-A"),
        )
        a_fact_ids = {
            h.memory_id for h in a_result.hits if h.memory_layer == "fact"
        }
        assert fa_id in a_fact_ids
        assert fb_id not in a_fact_ids

    @pytest.mark.asyncio
    async def test_search_facts_keyword_bm25_surfaces_literal_match(
        self, pg_store,
    ) -> None:
        """M31c — Postgres BM25 over fact_text catches literal keyword
        matches that semantic embeddings tend to under-weight.

        Seeds a fact with the literal phrase 'Philips LED bulb' then
        queries with the same phrase; expects the fact in the BM25
        results. Mirrors the SSU bench failure shape: specific factoid
        questions need keyword retrieval, not just semantic."""
        import uuid

        from astrocyte.types import MemoryFact, PageIndexDocument, PageIndexSection

        doc_id = await pg_store.save_document(
            PageIndexDocument(
                id="", bank_id="b1", source_id="u1", md_text="",
                reference_date=None,
                built_at=datetime.now(tz=timezone.utc),
            ),
        )
        await pg_store.save_sections(doc_id, [
            PageIndexSection(
                document_id=doc_id, line_num=1, node_id="s1",
                title="lamp", summary="bedside lamp tips",
            ),
        ])
        target_id = str(uuid.uuid4())
        distractor_id = str(uuid.uuid4())
        await pg_store.save_facts([
            MemoryFact(
                id=target_id, bank_id="b1", document_id=doc_id, line_num=1,
                text="User replaced the bedside lamp bulb with a Philips LED bulb.",
                fact_type="experience", entities=["Philips"],
                embedding=_vec_dim0(),
            ),
            MemoryFact(
                id=distractor_id, bank_id="b1", document_id=doc_id, line_num=1,
                text="Assistant recommended layered lighting for a cozy nook.",
                fact_type="preference", entities=["lighting"],
                embedding=_vec_dim0(),
            ),
        ])

        # BM25 query for the literal keywords.
        hits = await pg_store.search_facts_keyword(
            "b1", "Philips LED bulb", top_k=10,
        )
        hit_ids = [h.fact_id for h in hits]
        # The target with the literal "Philips LED" match must rank first.
        assert hit_ids[0] == target_id
        # Distractor either ranks lower or doesn't appear (depending on tsvector match).
        if distractor_id in hit_ids:
            assert hit_ids.index(distractor_id) > hit_ids.index(target_id)

    @pytest.mark.asyncio
    async def test_pipeline_carries_event_date_through_postgres(
        self, pg_store,
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from astrocyte.pipeline.pageindex_pipeline import PageIndexPipeline
        from astrocyte.types import RecallRequest

        _, fa_id, fb_id = await _seed_two_sessions(pg_store)
        provider = MagicMock()
        provider.embed = AsyncMock(return_value=[_vec_dim0()])
        pipeline = PageIndexPipeline(
            store=pg_store, embedding_provider=provider,
        )

        result = await pipeline.recall(
            RecallRequest(query="alpha", bank_id="b1"),
        )
        fa = next(h for h in result.hits if h.memory_id == fa_id)
        assert fa.metadata["event_date"] == datetime(2024, 5, 5, tzinfo=timezone.utc)
        assert fa.occurred_at == datetime(2024, 5, 5, tzinfo=timezone.utc)
        fb = next(h for h in result.hits if h.memory_id == fb_id)
        assert fb.metadata["event_date"] is None
        assert fb.occurred_at is None
