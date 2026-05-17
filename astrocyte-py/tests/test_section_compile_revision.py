"""M12.6: Karpathy incremental wiki update — unit tests.

Two surfaces:

1. ``_revise_observation`` — single LLM call returns the revised
   (title, content) when the judge flags REVISE, else ``None``.

2. ``revise_wikis_for_document`` — orchestration that loads existing
   wikis, resolves provenance to sections, sorts chronologically,
   calls _revise_observation, and persists revisions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from astrocyte.pipeline.section_compile import (
    _revise_observation,
    revise_wikis_for_document,
)
from astrocyte.testing.in_memory import InMemoryPageIndexStore
from astrocyte.types import (
    Completion,
    PageIndexDocument,
    PageIndexSection,
    WikiPage,
)


def _section(line_num: int, summary: str, *, date_str: str) -> PageIndexSection:
    return PageIndexSection(
        document_id="doc-1",
        line_num=line_num,
        node_id=f"{line_num:04d}",
        title=f"node-{line_num}",
        summary=summary,
        summary_embedding=[0.1] * 10,
        session_date=datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc),
    )


def _wiki_page(*, title: str, content: str, source_lines: list[int], revision: int = 1) -> WikiPage:
    return WikiPage(
        page_id=f"obs:doc-1:{title.lower().replace(' ', '-')}",
        bank_id="b1",
        kind="topic",
        title=title,
        content=content,
        scope="document:doc-1",
        source_ids=[f"doc-1:{ln}" for ln in source_lines],
        cross_links=[],
        revision=revision,
        revised_at=datetime.now(tz=timezone.utc),
    )


def _mock_complete(text: str) -> MagicMock:
    p = MagicMock()
    p.complete = AsyncMock(return_value=Completion(text=text, model="gpt-4o-mini"))
    p.embed = AsyncMock(return_value=[[0.2] * 10])
    return p


class TestReviseObservation:
    async def test_empty_sections_returns_none(self) -> None:
        provider = MagicMock()
        out = await _revise_observation(
            provider=provider,
            model="gpt-4o-mini",
            page=_wiki_page(title="T", content="c", source_lines=[]),
            sections=[],
        )
        assert out is None

    async def test_empty_content_returns_none(self) -> None:
        provider = MagicMock()
        out = await _revise_observation(
            provider=provider,
            model="gpt-4o-mini",
            page=_wiki_page(title="T", content="   ", source_lines=[1]),
            sections=[_section(1, "s", date_str="2023-01-01")],
        )
        assert out is None

    async def test_ok_verdict_returns_none(self) -> None:
        provider = _mock_complete('{"verdict": "OK"}')
        out = await _revise_observation(
            provider=provider,
            model="gpt-4o-mini",
            page=_wiki_page(title="Doctors", content="User saw 1 doctor.", source_lines=[1]),
            sections=[_section(1, "saw Dr. Patel", date_str="2023-01-01")],
        )
        assert out is None

    async def test_revise_returns_new_title_and_content(self) -> None:
        provider = _mock_complete(
            '{"verdict": "REVISE", "revised_title": "Doctors visited", '
            '"revised_content": "User saw 2 doctors: Dr. Patel (Jan), Dr. Lee (Mar)."}'
        )
        out = await _revise_observation(
            provider=provider,
            model="gpt-4o-mini",
            page=_wiki_page(title="Doctor", content="User saw 1 doctor.", source_lines=[1, 2]),
            sections=[
                _section(1, "saw Dr. Patel", date_str="2023-01-01"),
                _section(2, "switched to Dr. Lee", date_str="2023-03-01"),
            ],
        )
        assert out is not None
        new_title, new_content = out
        assert new_title == "Doctors visited"
        assert "2 doctors" in new_content

    async def test_revise_with_missing_fields_returns_none(self) -> None:
        # Defensive: judge returned REVISE but didn't supply title/content
        provider = _mock_complete('{"verdict": "REVISE"}')
        out = await _revise_observation(
            provider=provider,
            model="gpt-4o-mini",
            page=_wiki_page(title="T", content="c", source_lines=[1]),
            sections=[_section(1, "s", date_str="2023-01-01")],
        )
        assert out is None

    async def test_revise_identical_to_existing_returns_none(self) -> None:
        provider = _mock_complete('{"verdict": "REVISE", "revised_title": "T", "revised_content": "c"}')
        out = await _revise_observation(
            provider=provider,
            model="gpt-4o-mini",
            page=_wiki_page(title="T", content="c", source_lines=[1]),
            sections=[_section(1, "s", date_str="2023-01-01")],
        )
        # Same title + content → treat as no-op
        assert out is None

    async def test_malformed_json_returns_none(self) -> None:
        provider = _mock_complete("not valid")
        out = await _revise_observation(
            provider=provider,
            model="gpt-4o-mini",
            page=_wiki_page(title="T", content="c", source_lines=[1]),
            sections=[_section(1, "s", date_str="2023-01-01")],
        )
        assert out is None

    async def test_llm_failure_returns_none(self) -> None:
        provider = MagicMock()
        provider.complete = AsyncMock(side_effect=RuntimeError("api down"))
        out = await _revise_observation(
            provider=provider,
            model="gpt-4o-mini",
            page=_wiki_page(title="T", content="c", source_lines=[1]),
            sections=[_section(1, "s", date_str="2023-01-01")],
        )
        assert out is None

    async def test_sections_rendered_in_caller_order(self) -> None:
        # The function does NOT re-sort sections — caller sorts. This
        # locks the contract so a future refactor that adds sorting
        # here can be caught.
        provider = _mock_complete('{"verdict": "OK"}')
        await _revise_observation(
            provider=provider,
            model="gpt-4o-mini",
            page=_wiki_page(title="T", content="content", source_lines=[1, 2]),
            sections=[
                _section(2, "second-text", date_str="2023-03-01"),
                _section(1, "first-text", date_str="2023-01-01"),
            ],
        )
        sent = provider.complete.call_args.args[0][0].content
        # The second section (passed first) should appear earlier in
        # the prompt — exact caller order preserved.
        assert sent.index("second-text") < sent.index("first-text")


class TestReviseWikisForDocument:
    async def test_no_existing_wikis_returns_zero(self) -> None:
        store = InMemoryPageIndexStore()
        doc = PageIndexDocument(
            id="",
            bank_id="b1",
            source_id="s1",
            md_text="# m",
        )
        await store.save_document(doc)
        out = await revise_wikis_for_document(
            store=store,
            bank_id="b1",
            document_id="doc-1",
            provider=MagicMock(),
        )
        assert out == 0

    async def test_revises_and_persists(self) -> None:
        store = InMemoryPageIndexStore()
        doc = PageIndexDocument(
            id="doc-1",
            bank_id="b1",
            source_id="s1",
            md_text="# m",
        )
        doc_id = await store.save_document(doc)
        await store.save_sections(
            doc_id,
            [
                _section(1, "saw Dr. Patel", date_str="2023-01-01"),
                _section(2, "switched to Dr. Lee", date_str="2023-03-01"),
            ],
        )
        page = _wiki_page(
            title="Doctor",
            content="User saw 1 doctor.",
            source_lines=[1, 2],
        )
        await store.save_wiki_page(page=page, embedding=[0.1] * 10, provenance=[(doc_id, 1), (doc_id, 2)])

        provider = _mock_complete(
            '{"verdict": "REVISE", "revised_title": "Doctors visited", '
            '"revised_content": "User saw 2 doctors: Dr. Patel, then Dr. Lee."}'
        )
        revised = await revise_wikis_for_document(
            store=store,
            bank_id="b1",
            document_id=doc_id,
            provider=provider,
        )
        assert revised == 1
        pages_after = await store.list_wiki_pages_for_doc("b1", doc_id)
        assert len(pages_after) == 1
        # In-memory store may or may not bump revision in place — at
        # minimum the content should be updated.
        assert "2 doctors" in pages_after[0].content
        assert pages_after[0].title == "Doctors visited"

    async def test_idempotent_on_ok_verdict(self) -> None:
        store = InMemoryPageIndexStore()
        doc_id = await store.save_document(
            PageIndexDocument(
                id="",
                bank_id="b1",
                source_id="s1",
                md_text="# m",
            )
        )
        await store.save_sections(doc_id, [_section(1, "s", date_str="2023-01-01")])
        page = _wiki_page(title="T", content="c", source_lines=[1])
        await store.save_wiki_page(page=page, embedding=[0.1] * 10, provenance=[(doc_id, 1)])

        provider = _mock_complete('{"verdict": "OK"}')
        revised = await revise_wikis_for_document(
            store=store,
            bank_id="b1",
            document_id=doc_id,
            provider=provider,
        )
        assert revised == 0

    async def test_handles_missing_provenance_section(self) -> None:
        # Wiki has provenance referencing a line that no longer exists.
        # Should skip cleanly, not crash.
        store = InMemoryPageIndexStore()
        doc_id = await store.save_document(
            PageIndexDocument(
                id="",
                bank_id="b1",
                source_id="s1",
                md_text="# m",
            )
        )
        await store.save_sections(doc_id, [_section(1, "s", date_str="2023-01-01")])
        # Page claims provenance from line 99 (doesn't exist)
        page = _wiki_page(title="T", content="c", source_lines=[99])
        await store.save_wiki_page(page=page, embedding=[0.1] * 10, provenance=[(doc_id, 99)])

        provider = _mock_complete('{"verdict": "OK"}')
        revised = await revise_wikis_for_document(
            store=store,
            bank_id="b1",
            document_id=doc_id,
            provider=provider,
        )
        # No sections resolved → nothing to revise against
        assert revised == 0
