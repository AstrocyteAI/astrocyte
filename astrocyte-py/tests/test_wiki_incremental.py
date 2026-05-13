"""M14.2: wiki_incremental unit tests (skeleton).

Covers the entry point's edge cases and graceful-degradation paths.
The substantive cases — entity-overlap query correctness, multi-wiki
update orchestration, idempotency on re-run — land once the SPI
``list_wikis_affected_by_entities`` is implemented in
``InMemoryPageIndexStore``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from astrocyte.pipeline.wiki_incremental import (
    IncrementalUpdateReport,
    update_affected_wikis_for_document,
)

pytestmark = pytest.mark.asyncio


class TestWikiIncrementalSkeleton:
    """Smoke tests against the skeleton implementation. The entity-
    overlap fallback returns empty; these tests verify the entry point's
    early-return guards behave correctly until the SPI lands."""

    async def test_no_entities_returns_empty_report(self) -> None:
        wiki_store = MagicMock()
        page_index_store = MagicMock()
        provider = MagicMock()
        provider.complete = AsyncMock()
        report = await update_affected_wikis_for_document(
            wiki_store=wiki_store,
            page_index_store=page_index_store,
            provider=provider,
            bank_id="b1",
            document_id="doc-1",
            new_entities=[],
            new_content_excerpts={},
        )
        assert isinstance(report, IncrementalUpdateReport)
        assert report.affected_count == 0
        assert report.updated == []
        provider.complete.assert_not_called()

    async def test_no_affected_wikis_returns_zero(self) -> None:
        """Skeleton's ``_list_wikis_affected_by_entities`` always returns
        empty until the SPI lands. Entry point must surface that as
        affected_count=0 with no LLM call attempted."""
        wiki_store = MagicMock()
        page_index_store = MagicMock()
        provider = MagicMock()
        provider.complete = AsyncMock()
        report = await update_affected_wikis_for_document(
            wiki_store=wiki_store,
            page_index_store=page_index_store,
            provider=provider,
            bank_id="b1",
            document_id="doc-1",
            new_entities=["Alice", "Bob"],
            new_content_excerpts={
                "Alice": "Alice moved to Berlin",
                "Bob": "Bob took up archery",
            },
        )
        assert report.affected_count == 0
        assert report.updated == []
        assert report.failed == []
        # No LLM call — skeleton's fallback found 0 affected wikis.
        provider.complete.assert_not_called()


# TODO(m14.2): once ``list_wikis_affected_by_entities`` is implemented
# on ``InMemoryPageIndexStore``, add:
#
#   - test_single_affected_wiki_updated  (1 wiki, UPDATE verdict, revision++)
#   - test_no_change_verdict_skips       (1 wiki, NO_CHANGE verdict, no save)
#   - test_top_n_capped                  (10 affected wikis, max_updates=5)
#   - test_idempotent_on_rerun           (2nd call sees revised wiki → NO_CHANGE)
#   - test_malformed_json_marks_failed   (bad LLM output → FAILED, no crash)
#   - test_llm_failure_marks_failed      (provider raises → FAILED, continues)
#   - test_min_overlap_filter            (overlap < threshold → skip)
#   - test_no_excerpt_for_shared_entity  (NO_CHANGE shortcut path)
