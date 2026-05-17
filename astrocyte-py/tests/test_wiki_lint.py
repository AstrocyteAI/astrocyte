"""M12.5: Karpathy wiki lint unit tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from astrocyte.pipeline.wiki_lint import (
    WikiLintIssue,
    WikiLintReport,
    lint_one_wiki,
    lint_wiki_pages,
)
from astrocyte.types import Completion


def _mock_provider(text: str) -> MagicMock:
    p = MagicMock()
    p.complete = AsyncMock(return_value=Completion(text=text, model="gpt-4o-mini"))
    return p


class TestLintReport:
    def test_is_clean_no_issues(self) -> None:
        r = WikiLintReport(bank_id="b1")
        assert r.is_clean("page-1")

    def test_is_clean_with_unrelated_issue(self) -> None:
        r = WikiLintReport(
            bank_id="b1",
            issues=[WikiLintIssue(page_id="page-2", kind="contradicted")],
        )
        assert r.is_clean("page-1")
        assert not r.is_clean("page-2")

    def test_kinds_for_returns_all(self) -> None:
        r = WikiLintReport(
            bank_id="b1",
            issues=[
                WikiLintIssue(page_id="p1", kind="contradicted"),
                WikiLintIssue(page_id="p1", kind="stale"),
            ],
        )
        assert r.kinds_for("p1") == {"contradicted", "stale"}


class TestLintOneWiki:
    async def test_empty_facts_returns_none(self) -> None:
        provider = MagicMock()
        out = await lint_one_wiki(
            page_id="p1",
            title="T",
            content="some content",
            facts=[],
            llm_provider=provider,
        )
        assert out is None
        # No LLM call when there are no facts to compare against
        provider.complete.assert_not_called() if hasattr(provider.complete, "assert_not_called") else None

    async def test_empty_content_returns_none(self) -> None:
        provider = MagicMock()
        out = await lint_one_wiki(
            page_id="p1",
            title="T",
            content="   ",
            facts=["fact"],
            llm_provider=provider,
        )
        assert out is None

    async def test_contradiction_flagged(self) -> None:
        provider = _mock_provider('{"verdict": "CONTRADICTED", "explanation": "wiki says X, fact says not-X"}')
        out = await lint_one_wiki(
            page_id="p1",
            title="User profile",
            content="User works at Google.",
            facts=["User works at Anthropic since March 2026."],
            llm_provider=provider,
        )
        assert out is not None
        assert out.page_id == "p1"
        assert out.kind == "contradicted"
        assert "X" in out.detail

    async def test_ok_returns_none(self) -> None:
        provider = _mock_provider('{"verdict": "OK"}')
        out = await lint_one_wiki(
            page_id="p1",
            title="T",
            content="some content",
            facts=["fact"],
            llm_provider=provider,
        )
        assert out is None

    async def test_case_insensitive_verdict(self) -> None:
        provider = _mock_provider('{"verdict": "contradicted", "explanation": "e"}')
        out = await lint_one_wiki(
            page_id="p1",
            title="T",
            content="c",
            facts=["f"],
            llm_provider=provider,
        )
        assert out is not None and out.kind == "contradicted"

    async def test_malformed_json_returns_none(self) -> None:
        provider = _mock_provider("not valid json")
        out = await lint_one_wiki(
            page_id="p1",
            title="T",
            content="c",
            facts=["f"],
            llm_provider=provider,
        )
        assert out is None

    async def test_llm_failure_returns_none(self) -> None:
        provider = MagicMock()
        provider.complete = AsyncMock(side_effect=RuntimeError("api down"))
        out = await lint_one_wiki(
            page_id="p1",
            title="T",
            content="c",
            facts=["f"],
            llm_provider=provider,
        )
        assert out is None

    async def test_unknown_verdict_returns_none(self) -> None:
        # Defensive — judge might return MAYBE / IDK / something else
        provider = _mock_provider('{"verdict": "MAYBE"}')
        out = await lint_one_wiki(
            page_id="p1",
            title="T",
            content="c",
            facts=["f"],
            llm_provider=provider,
        )
        assert out is None

    async def test_facts_block_includes_all_facts(self) -> None:
        provider = _mock_provider('{"verdict": "OK"}')
        await lint_one_wiki(
            page_id="p1",
            title="T",
            content="c",
            facts=["alpha", "beta", "gamma"],
            llm_provider=provider,
        )
        # Inspect the prompt to confirm facts are numbered + included
        sent_prompt = provider.complete.call_args.args[0]
        assert "1. alpha" in sent_prompt
        assert "2. beta" in sent_prompt
        assert "3. gamma" in sent_prompt


class TestLintWikiPages:
    async def test_orphan_flagged_when_no_facts(self) -> None:
        provider = MagicMock()
        report = await lint_wiki_pages(
            pages=[("p1", "T", "content", [])],
            llm_provider=provider,
            bank_id="b1",
        )
        assert len(report.issues) == 1
        assert report.issues[0].page_id == "p1"
        assert report.issues[0].kind == "orphan"

    async def test_mixed_orphan_and_contradiction(self) -> None:
        provider = _mock_provider('{"verdict": "CONTRADICTED", "explanation": "conflict"}')
        report = await lint_wiki_pages(
            pages=[
                ("p1", "T1", "content", []),  # orphan
                ("p2", "T2", "content", ["fact"]),  # contradicted
            ],
            llm_provider=provider,
            bank_id="b1",
        )
        kinds = {(i.page_id, i.kind) for i in report.issues}
        assert kinds == {("p1", "orphan"), ("p2", "contradicted")}

    async def test_all_clean_returns_empty_report(self) -> None:
        provider = _mock_provider('{"verdict": "OK"}')
        report = await lint_wiki_pages(
            pages=[
                ("p1", "T", "c", ["fact-a"]),
                ("p2", "T", "c", ["fact-b"]),
            ],
            llm_provider=provider,
            bank_id="b1",
        )
        assert report.issues == []
        assert report.is_clean("p1")
        assert report.is_clean("p2")

    async def test_blank_page_id_skipped(self) -> None:
        provider = MagicMock()
        report = await lint_wiki_pages(
            pages=[("", "T", "c", ["f"])],
            llm_provider=provider,
            bank_id="b1",
        )
        # Blank id → silent skip, no issues
        assert report.issues == []
