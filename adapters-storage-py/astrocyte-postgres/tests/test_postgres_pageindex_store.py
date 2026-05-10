"""PostgresPageIndexStore conformance against a live Postgres (M9 PR1).

Mirrors the in-memory conformance suite at
``astrocyte-py/tests/test_pageindex_store.py::TestInMemoryPageIndexStore``.
The same behaviours are pinned against a real DB:

- upsert keyed on ``(bank_id, source_id)`` (rebuild replaces in place)
- ``save_sections`` is atomic-replace (FK ON DELETE CASCADE wipes
  entities/links rows for the document)
- ``load_skeleton`` projects out the summary_embedding column
- entity / link upserts are idempotent on their composite PKs

Skipped when ``DATABASE_URL`` is unset so CI without a Postgres service
collects clean. Run locally with:

  DATABASE_URL=postgres://... uv run pytest tests/test_postgres_pageindex_store.py
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set (live Postgres test)",
)

from astrocyte.types import (  # noqa: E402  — import after pytestmark skip
    PageIndexDocument,
    PageIndexSection,
    PageIndexSectionEntity,
    PageIndexSectionLink,
)

from astrocyte_postgres.pageindex_store import PostgresPageIndexStore  # noqa: E402


def _doc(bank_id: str, source_id: str = "conv-test", md: str = "# md") -> PageIndexDocument:
    return PageIndexDocument(
        id="",
        bank_id=bank_id,
        source_id=source_id,
        md_text=md,
        reference_date=datetime(2023, 5, 8, tzinfo=timezone.utc),
        built_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )


def _section(
    document_id: str,
    line_num: int,
    *,
    node_id: str | None = None,
    parent_node: str | None = None,
    depth: int = 0,
    summary: str | None = None,
) -> PageIndexSection:
    return PageIndexSection(
        document_id=document_id,
        line_num=line_num,
        node_id=node_id or f"{line_num:04d}",
        title=f"node-{line_num}",
        summary=summary,
        speaker=None,
        session_date=None,
        parent_node=parent_node,
        depth=depth,
    )


@pytest.fixture
async def store() -> PostgresPageIndexStore:
    """Fresh store + a unique bank_id per test (so tests can run in
    parallel without clobbering each other)."""
    s = PostgresPageIndexStore(bootstrap_schema=True)
    yield s
    # No teardown — bank_id isolation per test handles cleanup. If you
    # want to wipe rows for repeatable runs, use
    # ``astrocyte.eval._state_reset.reset_benchmark_state``.


@pytest.fixture
def bank_id() -> str:
    return f"test-pi-{uuid.uuid4().hex[:8]}"


class TestPostgresPageIndexStore:
    async def test_save_load_document_roundtrip(
        self, store: PostgresPageIndexStore, bank_id: str,
    ) -> None:
        doc_id = await store.save_document(_doc(bank_id, md="hello"))
        assert doc_id  # store-assigned UUID

        loaded = await store.load_document(bank_id, "conv-test")
        assert loaded is not None
        assert loaded.md_text == "hello"
        assert loaded.bank_id == bank_id
        assert loaded.id == doc_id

    async def test_load_document_returns_none_when_missing(
        self, store: PostgresPageIndexStore, bank_id: str,
    ) -> None:
        assert await store.load_document(bank_id, "no-such-conv") is None

    async def test_upsert_keyed_on_bank_id_source_id(
        self, store: PostgresPageIndexStore, bank_id: str,
    ) -> None:
        first_id = await store.save_document(_doc(bank_id, md="v1"))
        second_id = await store.save_document(_doc(bank_id, md="v2"))
        assert first_id == second_id, "upsert must preserve document id"
        loaded = await store.load_document(bank_id, "conv-test")
        assert loaded is not None and loaded.md_text == "v2"

    async def test_save_sections_atomic_replace(
        self, store: PostgresPageIndexStore, bank_id: str,
    ) -> None:
        doc_id = await store.save_document(_doc(bank_id))
        await store.save_sections(doc_id, [
            _section(doc_id, 1), _section(doc_id, 5), _section(doc_id, 10),
        ])
        skel = await store.load_skeleton(doc_id)
        assert [s.line_num for s in skel] == [1, 5, 10]

        # Replace with a smaller tree.
        await store.save_sections(doc_id, [_section(doc_id, 100), _section(doc_id, 200)])
        skel2 = await store.load_skeleton(doc_id)
        assert [s.line_num for s in skel2] == [100, 200]

    async def test_save_sections_cascades_to_entities_and_links(
        self, store: PostgresPageIndexStore, bank_id: str,
    ) -> None:
        # Pin the FK ON DELETE CASCADE in migration 015 — replacing the
        # tree must wipe dependent rows, not orphan them.
        doc_id = await store.save_document(_doc(bank_id))
        await store.save_sections(doc_id, [_section(doc_id, 1), _section(doc_id, 5)])
        await store.save_section_entities([
            PageIndexSectionEntity(document_id=doc_id, line_num=1, entity_name="Alice"),
        ])
        await store.save_section_links([
            PageIndexSectionLink(
                from_doc=doc_id, from_line=1,
                to_doc=doc_id, to_line=5,
                link_type="semantic_knn", weight=0.9,
            ),
        ])
        # Replace tree → cascade should drop the entity and link rows.
        await store.save_sections(doc_id, [_section(doc_id, 99)])

        # Verify via raw SQL that the rows are gone (the SPI doesn't
        # expose a "list entities/links" reader at PR1; PR2 will).
        pool = await store._ensure_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT COUNT(*) FROM {store._fq('astrocyte_pi_section_entities')} WHERE document_id = %s",
                    (doc_id,),
                )
                ent_count = (await cur.fetchone())[0]
                await cur.execute(
                    f"SELECT COUNT(*) FROM {store._fq('astrocyte_pi_section_links')} WHERE from_doc = %s",
                    (doc_id,),
                )
                link_count = (await cur.fetchone())[0]
        assert ent_count == 0, "entities must cascade-delete on tree replace"
        assert link_count == 0, "links must cascade-delete on tree replace"

    async def test_load_skeleton_orders_by_line_num(
        self, store: PostgresPageIndexStore, bank_id: str,
    ) -> None:
        doc_id = await store.save_document(_doc(bank_id))
        await store.save_sections(doc_id, [
            _section(doc_id, 50),
            _section(doc_id, 1),
            _section(doc_id, 12),
        ])
        skel = await store.load_skeleton(doc_id)
        assert [s.line_num for s in skel] == [1, 12, 50]

    async def test_save_section_links_rejects_invalid_link_type(
        self, store: PostgresPageIndexStore, bank_id: str,
    ) -> None:
        # The SQL CHECK constraint catches this server-side; the Python
        # adapter validates first to give a better error message.
        doc_id = await store.save_document(_doc(bank_id))
        await store.save_sections(doc_id, [_section(doc_id, 1), _section(doc_id, 5)])
        bad = PageIndexSectionLink(
            from_doc=doc_id, from_line=1,
            to_doc=doc_id, to_line=5,
            link_type="invalid_type", weight=1.0,
        )
        with pytest.raises(ValueError, match="link_type"):
            await store.save_section_links([bad])

    async def test_save_section_links_idempotent(
        self, store: PostgresPageIndexStore, bank_id: str,
    ) -> None:
        doc_id = await store.save_document(_doc(bank_id))
        await store.save_sections(doc_id, [_section(doc_id, 1), _section(doc_id, 5)])
        link = PageIndexSectionLink(
            from_doc=doc_id, from_line=1, to_doc=doc_id, to_line=5,
            link_type="semantic_knn", weight=0.85,
        )
        # ON CONFLICT DO UPDATE — second call must not error.
        await store.save_section_links([link])
        await store.save_section_links([link])

    async def test_save_section_entities_idempotent(
        self, store: PostgresPageIndexStore, bank_id: str,
    ) -> None:
        doc_id = await store.save_document(_doc(bank_id))
        await store.save_sections(doc_id, [_section(doc_id, 1)])
        e = PageIndexSectionEntity(document_id=doc_id, line_num=1, entity_name="Alice")
        # ON CONFLICT DO NOTHING — second call must not error.
        await store.save_section_entities([e])
        await store.save_section_entities([e])

    async def test_save_empty_lists_noop(
        self, store: PostgresPageIndexStore, bank_id: str,
    ) -> None:
        doc_id = await store.save_document(_doc(bank_id))
        assert await store.save_sections(doc_id, []) == 0
        assert await store.save_section_entities([]) == 0
        assert await store.save_section_links([]) == 0

    async def test_health(self, store: PostgresPageIndexStore) -> None:
        h = await store.health()
        assert h.healthy is True
