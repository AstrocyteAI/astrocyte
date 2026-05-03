"""Integration tests for the Postgres WikiStore."""

from __future__ import annotations

from datetime import datetime, timezone

from astrocyte.types import WikiPage

from astrocyte_postgres.wiki_store import PostgresWikiStore


async def test_pg_wiki_store_roundtrip(dsn: str) -> None:
    store = PostgresWikiStore(dsn=dsn)
    page = WikiPage(
        page_id="person:alice",
        bank_id="bank-1",
        kind="entity",
        title="Alice",
        content="## Alice\n\nAlice likes hiking.",
        scope="Alice",
        source_ids=["m1", "m2"],
        cross_links=["topic:hiking"],
        revision=1,
        revised_at=datetime(2026, 4, 26, tzinfo=timezone.utc),
        tags=["person"],
        metadata={"confidence": 0.9},
    )

    try:
        await store.upsert_page(page, "bank-1")

        stored = await store.get_page("person:alice", "bank-1")

        assert stored is not None
        assert stored.page_id == "person:alice"
        assert stored.revision == 1
        assert stored.source_ids == ["m1", "m2"]
        assert stored.cross_links == ["topic:hiking"]
        assert stored.metadata == {"confidence": 0.9}
    finally:
        await store.delete_page("person:alice", "bank-1")
        await store.close()


async def test_pg_wiki_store_increments_revision(dsn: str) -> None:
    store = PostgresWikiStore(dsn=dsn)
    base = WikiPage(
        page_id="topic:deployments",
        bank_id="bank-1",
        kind="topic",
        title="Deployments",
        content="## Deployments\n\nInitial.",
        scope="deployments",
        source_ids=["m1"],
        cross_links=[],
        revision=1,
        revised_at=datetime(2026, 4, 26, tzinfo=timezone.utc),
    )
    updated = WikiPage(
        **{
            **base.__dict__,
            "content": "## Deployments\n\nUpdated.",
            "source_ids": ["m1", "m2"],
        }
    )

    try:
        await store.upsert_page(base, "bank-1")
        await store.upsert_page(updated, "bank-1")

        stored = await store.get_page("topic:deployments", "bank-1")

        assert stored is not None
        assert stored.revision == 2
        assert stored.content.endswith("Updated.")
        assert stored.source_ids == ["m1", "m2"]
    finally:
        await store.delete_page("topic:deployments", "bank-1")
        await store.close()
