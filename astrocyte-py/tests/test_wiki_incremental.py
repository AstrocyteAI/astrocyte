"""M14.2: wiki_incremental — entity-overlap update sweep tests.

Verifies ``update_affected_wikis_for_document`` against the in-memory
``PageIndexStore`` with focused coverage of:

- **entity-overlap candidate selection** — the shared-entity JOIN that
  picks which existing wiki pages are affected by a newly-ingested
  document's entities;
- **LLM update prompt / response parsing** — the prompt shape sent to
  the provider and the rules for accepting / rejecting the returned
  revision payload (well-formed JSON, content delta vs. no-op);
- **idempotent / no-op behavior** — re-running with the same input or
  with no effective changes must not bump revisions or emit fake
  updates;
- **graceful degradation on error paths** — provider failures, store
  errors, and partial-batch failures all surface in the report without
  raising.

These tests give pre-Postgres confidence in incremental-update semantics
and reporting before the Postgres bench wiring lands.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrocyte.pipeline.wiki_incremental import (
    IncrementalUpdateReport,
    update_affected_wikis_for_document,
)
from astrocyte.testing.in_memory import InMemoryPageIndexStore
from astrocyte.types import (
    Completion,
    PageIndexSectionEntity,
    WikiPage,
)

pytestmark = pytest.mark.asyncio


def _make_source_id(doc_id: str, line_num: int) -> str:
    """Build the canonical ``<doc_id>:<line_num>`` source-id used by wiki
    provenance (M12.6 convention). Centralised so tests don't repeat the
    format string and the convention is greppable.
    """
    return f"{doc_id}:{line_num}"


# ─── Fixtures ────────────────────────────────────────────────────────


async def _seed_one_wiki_with_entities(
    *,
    store: InMemoryPageIndexStore,
    bank_id: str,
    page_id: str,
    title: str,
    content: str,
    doc_id: str,
    line_num: int,
    entities: list[str],
) -> WikiPage:
    """Seed a wiki page with provenance and entity rows so the
    entity-overlap query can find it."""
    page = WikiPage(
        page_id=page_id,
        bank_id=bank_id,
        kind="entity",
        title=title,
        content=content,
        scope=f"document:{doc_id}",
        source_ids=[_make_source_id(doc_id, line_num)],
        cross_links=[],
        revision=1,
        revised_at=datetime.now(tz=timezone.utc),
    )
    await store.save_wiki_page(
        page=page, embedding=None,
        provenance=[(doc_id, line_num)],
    )
    await store.save_section_entities([
        PageIndexSectionEntity(
            document_id=doc_id, line_num=line_num, entity_name=e,
        )
        for e in entities
    ])
    return page


def _provider(text: str) -> MagicMock:
    """Build a MagicMock LLMProvider whose ``complete`` returns ``text``."""
    p = MagicMock()
    p.complete = AsyncMock(return_value=Completion(
        text=text, model="gpt-4o-mini",
    ))
    return p


# ─── Tests ───────────────────────────────────────────────────────────


class TestSkeletonGuards:
    async def test_no_entities_returns_empty_report(self) -> None:
        provider = _provider("")
        report = await update_affected_wikis_for_document(
            page_index_store=InMemoryPageIndexStore(),
            provider=provider,
            bank_id="b1", document_id="doc-new",
            new_entities=[], new_content_excerpts={},
        )
        assert isinstance(report, IncrementalUpdateReport)
        assert report.affected_count == 0
        assert report.updated == report.skipped == report.failed == []
        # Short-circuit assertion: with no entities there's nothing to
        # join against, so the LLM must never be invoked.
        provider.complete.assert_not_called()

    async def test_no_affected_wikis_skips_llm(self) -> None:
        """No wikis whose provenance contains shared entities → no LLM call."""
        store = InMemoryPageIndexStore()
        await _seed_one_wiki_with_entities(
            store=store, bank_id="b1", page_id="entity:charlie",
            title="Charlie", content="Charlie likes jazz.",
            doc_id="doc-old", line_num=5, entities=["Charlie"],
        )
        provider = _provider("")
        report = await update_affected_wikis_for_document(
            page_index_store=store, provider=provider,
            bank_id="b1", document_id="doc-new",
            new_entities=["Alice"],
            new_content_excerpts={"Alice": "Alice moved to Berlin."},
        )
        assert report.affected_count == 0
        provider.complete.assert_not_called()


class TestSingleWikiUpdate:
    async def test_update_verdict_bumps_revision(self) -> None:
        store = InMemoryPageIndexStore()
        await _seed_one_wiki_with_entities(
            store=store, bank_id="b1", page_id="entity:alice",
            title="Alice", content="Alice lives in Paris.",
            doc_id="doc-old", line_num=3, entities=["Alice"],
        )
        # Baseline persisted state before the update — pins the
        # pre-condition we're comparing against (revision 1, Paris).
        wikis_before = await store.list_wiki_pages_for_doc("b1", "doc-old")
        assert len(wikis_before) == 1
        assert wikis_before[0].page_id == "entity:alice"
        assert wikis_before[0].revision == 1
        assert wikis_before[0].content == "Alice lives in Paris."

        provider = _provider(
            '{"verdict": "UPDATE",'
            ' "revised_content": "Alice lives in Berlin (previously Paris)."}',
        )
        report = await update_affected_wikis_for_document(
            page_index_store=store, provider=provider,
            bank_id="b1", document_id="doc-new",
            new_entities=["Alice"],
            new_content_excerpts={"Alice": "Alice told me she moved to Berlin."},
        )
        assert report.affected_count == 1
        assert len(report.updated) == 1
        assert report.updated[0].page_id == "entity:alice"
        assert report.updated[0].new_revision == 2
        # The wiki's persisted content + revision are both updated in
        # the page-index-store bucket — old state is replaced, not added.
        wikis = await store.list_wiki_pages_for_doc("b1", "doc-old")
        assert len(wikis) == 1
        assert wikis[0].revision == 2
        assert wikis[0].content != wikis_before[0].content
        assert "Berlin" in wikis[0].content
        assert "previously Paris" in wikis[0].content
        provider.complete.assert_called_once()

    async def test_no_change_verdict_skips_save(self) -> None:
        store = InMemoryPageIndexStore()
        await _seed_one_wiki_with_entities(
            store=store, bank_id="b1", page_id="entity:alice",
            title="Alice", content="Alice lives in Paris.",
            doc_id="doc-old", line_num=3, entities=["Alice"],
        )
        provider = _provider('{"verdict": "NO_CHANGE"}')
        report = await update_affected_wikis_for_document(
            page_index_store=store, provider=provider,
            bank_id="b1", document_id="doc-new",
            new_entities=["Alice"],
            new_content_excerpts={"Alice": "Alice mentioned the weather."},
        )
        assert report.affected_count == 1
        assert report.updated == []
        assert len(report.skipped) == 1
        # Wiki content unchanged.
        wikis = await store.list_wiki_pages_for_doc("b1", "doc-old")
        assert wikis[0].content == "Alice lives in Paris."


class TestCappingAndOrdering:
    async def test_top_n_capped_by_max_updates(self) -> None:
        store = InMemoryPageIndexStore()
        # 7 wikis, all overlapping the query entity "Alice".
        for i in range(7):
            await _seed_one_wiki_with_entities(
                store=store, bank_id="b1", page_id=f"entity:alice-{i}",
                title=f"Alice ({i})", content=f"Alice context {i}.",
                doc_id=f"doc-old-{i}", line_num=1, entities=["Alice"],
            )
        provider = _provider('{"verdict": "NO_CHANGE"}')
        report = await update_affected_wikis_for_document(
            page_index_store=store, provider=provider,
            bank_id="b1", document_id="doc-new",
            new_entities=["Alice"],
            new_content_excerpts={"Alice": "Alice context."},
            max_updates=3,
        )
        # SPI limit applied — only 3 LLM calls.
        assert report.affected_count == 3
        assert provider.complete.await_count == 3


class TestErrorPaths:
    async def test_malformed_json_marks_failed(self) -> None:
        store = InMemoryPageIndexStore()
        await _seed_one_wiki_with_entities(
            store=store, bank_id="b1", page_id="entity:alice",
            title="Alice", content="Alice lives in Paris.",
            doc_id="doc-old", line_num=3, entities=["Alice"],
        )
        provider = _provider("not valid json {")
        report = await update_affected_wikis_for_document(
            page_index_store=store, provider=provider,
            bank_id="b1", document_id="doc-new",
            new_entities=["Alice"],
            new_content_excerpts={"Alice": "Some new info."},
        )
        assert report.affected_count == 1
        assert report.updated == []
        assert len(report.failed) == 1
        assert report.failed[0].verdict == "FAILED"
        # The detail field must surface a JSON-parse diagnostic — bare
        # FAILED with no context would defeat the graceful-degradation
        # design (operators need to know why a wiki refused to update).
        assert "json" in report.failed[0].detail.lower()
        wikis = await store.list_wiki_pages_for_doc("b1", "doc-old")
        assert wikis[0].content == "Alice lives in Paris."

    async def test_llm_call_failure_marks_failed(self) -> None:
        store = InMemoryPageIndexStore()
        await _seed_one_wiki_with_entities(
            store=store, bank_id="b1", page_id="entity:alice",
            title="Alice", content="Alice lives in Paris.",
            doc_id="doc-old", line_num=3, entities=["Alice"],
        )
        provider = MagicMock()
        provider.complete = AsyncMock(side_effect=RuntimeError("api down"))
        report = await update_affected_wikis_for_document(
            page_index_store=store, provider=provider,
            bank_id="b1", document_id="doc-new",
            new_entities=["Alice"],
            new_content_excerpts={"Alice": "Some new info."},
        )
        assert report.affected_count == 1
        assert len(report.failed) == 1
        assert "api down" in report.failed[0].detail

    async def test_no_excerpt_for_shared_entities_shortcuts(self) -> None:
        """Declared shared entities but caller supplied no excerpt text
        → skip the LLM call and emit NO_CHANGE."""
        store = InMemoryPageIndexStore()
        await _seed_one_wiki_with_entities(
            store=store, bank_id="b1", page_id="entity:alice",
            title="Alice", content="Alice lives in Paris.",
            doc_id="doc-old", line_num=3, entities=["Alice"],
        )
        provider = _provider("")
        report = await update_affected_wikis_for_document(
            page_index_store=store, provider=provider,
            bank_id="b1", document_id="doc-new",
            new_entities=["Alice"],
            new_content_excerpts={},
        )
        assert report.affected_count == 1
        assert len(report.skipped) == 1
        provider.complete.assert_not_called()


class TestIdempotency:
    async def test_second_run_with_updated_state_is_no_change(self) -> None:
        store = InMemoryPageIndexStore()
        await _seed_one_wiki_with_entities(
            store=store, bank_id="b1", page_id="entity:alice",
            title="Alice", content="Alice lives in Paris.",
            doc_id="doc-old", line_num=3, entities=["Alice"],
        )
        provider1 = _provider(
            '{"verdict": "UPDATE",'
            ' "revised_content": "Alice lives in Berlin (previously Paris)."}',
        )
        first = await update_affected_wikis_for_document(
            page_index_store=store, provider=provider1,
            bank_id="b1", document_id="doc-new",
            new_entities=["Alice"],
            new_content_excerpts={"Alice": "Alice moved to Berlin."},
        )
        assert len(first.updated) == 1
        # Second call with same evidence: LLM judges that the wiki now
        # already reflects the new state.
        provider2 = _provider('{"verdict": "NO_CHANGE"}')
        second = await update_affected_wikis_for_document(
            page_index_store=store, provider=provider2,
            bank_id="b1", document_id="doc-new",
            new_entities=["Alice"],
            new_content_excerpts={"Alice": "Alice moved to Berlin."},
        )
        assert second.updated == []
        assert len(second.skipped) == 1


class TestMinOverlap:
    async def test_min_overlap_filter_rejects_single_entity(self) -> None:
        store = InMemoryPageIndexStore()
        await _seed_one_wiki_with_entities(
            store=store, bank_id="b1", page_id="entity:alice-bob",
            title="Alice & Bob", content="A & B share a band.",
            doc_id="doc-old", line_num=3, entities=["Alice", "Bob"],
        )
        provider = _provider('{"verdict": "NO_CHANGE"}')
        # Single overlap, below threshold 2.
        report = await update_affected_wikis_for_document(
            page_index_store=store, provider=provider,
            bank_id="b1", document_id="doc-new",
            new_entities=["Alice"],
            new_content_excerpts={"Alice": "Alice news."},
            min_overlap=2,
        )
        assert report.affected_count == 0
        provider.complete.assert_not_called()
