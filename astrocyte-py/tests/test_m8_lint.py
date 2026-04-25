"""M8 W6: Lint pass — unit tests.

Tests cover:
- LintIssue / LintResult dataclasses
- LintEngine._check_stale_or_orphan:
    - No issue when all sources are live
    - Stale when some sources missing
    - Orphan when all sources missing
    - No issue when page has no source_ids
- LintEngine.run (integration with InMemory stores):
    - Clean bank → no issues
    - Stale page detected
    - Orphan page detected
    - Mixed (some pages clean, some stale)
    - Bank isolation (issues in bank1 don't affect bank2)
    - Error path returns LintResult with error field
- Contradiction detection:
    - LLM responds "CONTRADICTION: ..." → two issues created (both pages)
    - LLM responds "OK" → no issues
    - LLM failure skips pair silently
    - detect_contradictions=False (default) → no LLM calls
    - max_contradiction_pairs limits how many pairs are checked
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrocyte.pipeline.lint import LintEngine, LintIssue, LintResult
from astrocyte.testing.in_memory import InMemoryVectorStore, InMemoryWikiStore, MockLLMProvider
from astrocyte.types import VectorItem, WikiPage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIM = 16


def _unit_vec(pos: int) -> list[float]:
    v = [0.0] * _DIM
    v[pos % _DIM] = 1.0
    return v


def _raw_item(item_id: str, bank_id: str) -> VectorItem:
    return VectorItem(
        id=item_id,
        bank_id=bank_id,
        vector=_unit_vec(hash(item_id) % _DIM),
        text=f"Raw memory: {item_id}",
        memory_layer="fact",
        fact_type="world",
    )


def _wiki_page(
    page_id: str,
    bank_id: str,
    source_ids: list[str] | None = None,
    title: str = "Test Page",
) -> WikiPage:
    return WikiPage(
        page_id=page_id,
        bank_id=bank_id,
        kind="topic",
        title=title,
        content=f"## {title}\n\nContent for {page_id}.",
        scope=page_id,
        source_ids=source_ids or [],
        cross_links=[],
        revision=1,
        revised_at=datetime.now(UTC),
    )


async def _setup_bank(
    vs: InMemoryVectorStore,
    ws: InMemoryWikiStore,
    bank_id: str,
    raw_ids: list[str],
    wiki_pages: list[WikiPage],
) -> None:
    """Store raw memories and wiki pages into the given stores."""
    if raw_ids:
        await vs.store_vectors([_raw_item(rid, bank_id) for rid in raw_ids])
    for page in wiki_pages:
        await ws.upsert_page(page, bank_id)


def _lint_engine(vs, ws, llm=None, *, detect_contradictions=False) -> LintEngine:
    return LintEngine(vs, ws, llm, detect_contradictions=detect_contradictions)


# ---------------------------------------------------------------------------
# LintIssue / LintResult structure
# ---------------------------------------------------------------------------


class TestLintDataclasses:
    def test_lint_issue_fields(self):
        issue = LintIssue(
            kind="stale",
            page_id="topic:foo",
            bank_id="bank1",
            detail="1 missing source",
            action="recompile",
        )
        assert issue.kind == "stale"
        assert issue.peer_page_id is None

    def test_lint_result_fields(self):
        r = LintResult(
            bank_id="bank1",
            pages_checked=3,
            stale_count=1,
            orphan_count=0,
            contradiction_count=0,
            issues=[],
            elapsed_ms=10,
        )
        assert r.error is None
        assert r.stale_count == 1


# ---------------------------------------------------------------------------
# _check_stale_or_orphan (unit)
# ---------------------------------------------------------------------------


class TestCheckStaleOrOrphan:
    def _engine(self):
        return LintEngine(MagicMock(), MagicMock())

    def test_no_issue_when_all_live(self):
        e = self._engine()
        page = _wiki_page("topic:foo", "bank1", source_ids=["m1", "m2"])
        result = e._check_stale_or_orphan(page, "bank1", live_ids={"m1", "m2"})
        assert result is None

    def test_stale_when_some_missing(self):
        e = self._engine()
        page = _wiki_page("topic:foo", "bank1", source_ids=["m1", "m2", "m3"])
        result = e._check_stale_or_orphan(page, "bank1", live_ids={"m1"})
        assert result is not None
        assert result.kind == "stale"
        assert result.action == "recompile"
        assert "2/3" in result.detail
        assert result.page_id == "topic:foo"

    def test_orphan_when_all_missing(self):
        e = self._engine()
        page = _wiki_page("topic:foo", "bank1", source_ids=["m1", "m2"])
        result = e._check_stale_or_orphan(page, "bank1", live_ids=set())
        assert result is not None
        assert result.kind == "orphan"
        assert result.action == "archive"

    def test_no_issue_when_no_source_ids(self):
        e = self._engine()
        page = _wiki_page("topic:foo", "bank1", source_ids=[])
        result = e._check_stale_or_orphan(page, "bank1", live_ids=set())
        assert result is None

    def test_stale_detail_truncates_long_missing_list(self):
        e = self._engine()
        source_ids = [f"m{i}" for i in range(10)]
        page = _wiki_page("topic:foo", "bank1", source_ids=source_ids)
        # Only keep 2 live
        result = e._check_stale_or_orphan(page, "bank1", live_ids={"m0", "m1"})
        assert result is not None
        assert result.kind == "stale"
        assert "…" in result.detail  # long list truncated


# ---------------------------------------------------------------------------
# LintEngine.run — integration
# ---------------------------------------------------------------------------


class TestLintEngineRun:
    @pytest.mark.asyncio
    async def test_clean_bank_no_issues(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        await _setup_bank(
            vs, ws, "bank1",
            raw_ids=["m1", "m2"],
            wiki_pages=[_wiki_page("topic:foo", "bank1", source_ids=["m1", "m2"])],
        )

        engine = _lint_engine(vs, ws)
        result = await engine.run("bank1")

        assert result.pages_checked == 1
        assert result.stale_count == 0
        assert result.orphan_count == 0
        assert result.issues == []
        assert result.error is None

    @pytest.mark.asyncio
    async def test_stale_page_detected(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        # Store 2 raw memories, wiki page cites 3 (one missing)
        await _setup_bank(
            vs, ws, "bank1",
            raw_ids=["m1", "m2"],
            wiki_pages=[_wiki_page("topic:foo", "bank1", source_ids=["m1", "m2", "m3-deleted"])],
        )

        engine = _lint_engine(vs, ws)
        result = await engine.run("bank1")

        assert result.stale_count == 1
        assert result.orphan_count == 0
        assert len(result.issues) == 1
        assert result.issues[0].kind == "stale"
        assert result.issues[0].page_id == "topic:foo"
        assert result.issues[0].action == "recompile"

    @pytest.mark.asyncio
    async def test_orphan_page_detected(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        # No raw memories stored; wiki page cites two deleted ones
        await _setup_bank(
            vs, ws, "bank1",
            raw_ids=[],
            wiki_pages=[_wiki_page("topic:foo", "bank1", source_ids=["m1-deleted", "m2-deleted"])],
        )

        engine = _lint_engine(vs, ws)
        result = await engine.run("bank1")

        assert result.orphan_count == 1
        assert result.stale_count == 0
        assert result.issues[0].kind == "orphan"
        assert result.issues[0].action == "archive"

    @pytest.mark.asyncio
    async def test_mixed_pages(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        await _setup_bank(
            vs, ws, "bank1",
            raw_ids=["m1"],
            wiki_pages=[
                _wiki_page("topic:clean", "bank1", source_ids=["m1"]),
                _wiki_page("topic:stale", "bank1", source_ids=["m1", "m2-gone"]),
                _wiki_page("topic:orphan", "bank1", source_ids=["m3-gone"]),
            ],
        )

        engine = _lint_engine(vs, ws)
        result = await engine.run("bank1")

        assert result.pages_checked == 3
        assert result.stale_count == 1
        assert result.orphan_count == 1
        assert len(result.issues) == 2

    @pytest.mark.asyncio
    async def test_empty_bank_no_issues(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        engine = _lint_engine(vs, ws)
        result = await engine.run("empty-bank")

        assert result.pages_checked == 0
        assert result.issues == []
        assert result.error is None

    @pytest.mark.asyncio
    async def test_bank_isolation(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        # bank1: clean; bank2: stale
        await _setup_bank(
            vs, ws, "bank1",
            raw_ids=["m1"],
            wiki_pages=[_wiki_page("topic:foo", "bank1", source_ids=["m1"])],
        )
        await _setup_bank(
            vs, ws, "bank2",
            raw_ids=[],
            wiki_pages=[_wiki_page("topic:bar", "bank2", source_ids=["m-gone"])],
        )

        engine = _lint_engine(vs, ws)

        r1 = await engine.run("bank1")
        assert r1.issues == []

        r2 = await engine.run("bank2")
        assert r2.orphan_count == 1

    @pytest.mark.asyncio
    async def test_page_with_no_source_ids_ignored(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        await _setup_bank(
            vs, ws, "bank1",
            raw_ids=[],
            wiki_pages=[_wiki_page("topic:nosources", "bank1", source_ids=[])],
        )

        engine = _lint_engine(vs, ws)
        result = await engine.run("bank1")

        assert result.issues == []

    @pytest.mark.asyncio
    async def test_error_path_returns_result_with_error(self):
        """When wiki_store.list_pages raises, run() returns an error result."""
        vs = InMemoryVectorStore()
        ws = MagicMock()
        ws.list_pages = AsyncMock(side_effect=RuntimeError("db down"))

        engine = _lint_engine(vs, ws)
        result = await engine.run("bank1")

        assert result.error is not None
        assert "db down" in result.error

    @pytest.mark.asyncio
    async def test_elapsed_ms_populated(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        engine = _lint_engine(vs, ws)
        result = await engine.run("bank1")

        assert result.elapsed_ms >= 0


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------


class TestContradictionDetection:
    def _mock_llm(self, verdict: str) -> MockLLMProvider:
        return MockLLMProvider(default_response=verdict)

    @pytest.mark.asyncio
    async def test_contradiction_detected(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        await _setup_bank(
            vs, ws, "bank1",
            raw_ids=["m1", "m2"],
            wiki_pages=[
                _wiki_page("topic:a", "bank1", source_ids=["m1"], title="Page A"),
                _wiki_page("topic:b", "bank1", source_ids=["m2"], title="Page B"),
            ],
        )

        llm = self._mock_llm("CONTRADICTION: Page A says X but Page B says not-X")
        engine = LintEngine(vs, ws, llm, detect_contradictions=True)
        result = await engine.run("bank1")

        assert result.contradiction_count == 2  # both pages flagged
        kinds = {i.kind for i in result.issues}
        assert "contradiction" in kinds
        # Both pages referenced
        page_ids = {i.page_id for i in result.issues}
        assert "topic:a" in page_ids
        assert "topic:b" in page_ids
        # peer_page_id cross-linked
        for issue in result.issues:
            if issue.kind == "contradiction":
                assert issue.peer_page_id is not None

    @pytest.mark.asyncio
    async def test_no_contradiction_when_ok(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        await _setup_bank(
            vs, ws, "bank1",
            raw_ids=["m1", "m2"],
            wiki_pages=[
                _wiki_page("topic:a", "bank1", source_ids=["m1"]),
                _wiki_page("topic:b", "bank1", source_ids=["m2"]),
            ],
        )

        llm = self._mock_llm("OK")
        engine = LintEngine(vs, ws, llm, detect_contradictions=True)
        result = await engine.run("bank1")

        contradiction_issues = [i for i in result.issues if i.kind == "contradiction"]
        assert len(contradiction_issues) == 0
        assert result.contradiction_count == 0

    @pytest.mark.asyncio
    async def test_contradiction_disabled_by_default(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        await _setup_bank(
            vs, ws, "bank1",
            raw_ids=["m1", "m2"],
            wiki_pages=[
                _wiki_page("topic:a", "bank1", source_ids=["m1"]),
                _wiki_page("topic:b", "bank1", source_ids=["m2"]),
            ],
        )

        # LLM would say CONTRADICTION, but detect_contradictions=False (default)
        llm = self._mock_llm("CONTRADICTION: they disagree")
        engine = LintEngine(vs, ws, llm, detect_contradictions=False)
        result = await engine.run("bank1")

        assert result.contradiction_count == 0

    @pytest.mark.asyncio
    async def test_llm_failure_skips_pair_silently(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        await _setup_bank(
            vs, ws, "bank1",
            raw_ids=["m1", "m2"],
            wiki_pages=[
                _wiki_page("topic:a", "bank1", source_ids=["m1"]),
                _wiki_page("topic:b", "bank1", source_ids=["m2"]),
            ],
        )

        llm = MagicMock()
        llm.complete = AsyncMock(side_effect=RuntimeError("LLM down"))
        engine = LintEngine(vs, ws, llm, detect_contradictions=True)
        result = await engine.run("bank1")

        # Should succeed; pair silently skipped
        assert result.error is None
        assert result.contradiction_count == 0

    @pytest.mark.asyncio
    async def test_max_contradiction_pairs_limits_checks(self):
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        # 5 pages → 10 pairs; set max_contradiction_pairs=3
        pages = [
            _wiki_page(f"topic:{i}", "bank1", source_ids=[f"m{i}"])
            for i in range(5)
        ]
        raw_ids = [f"m{i}" for i in range(5)]
        await _setup_bank(vs, ws, "bank1", raw_ids=raw_ids, wiki_pages=pages)

        llm = MagicMock()
        call_count = 0

        async def _mock_complete(messages, model=None, max_tokens=80):
            nonlocal call_count
            call_count += 1
            from astrocyte.types import Completion
            return Completion(text="OK", model="mock")

        llm.complete = _mock_complete
        engine = LintEngine(vs, ws, llm, detect_contradictions=True, max_contradiction_pairs=3)
        await engine.run("bank1")

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_single_page_no_contradiction_check(self):
        """With only one page there are no pairs to compare."""
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        await _setup_bank(
            vs, ws, "bank1",
            raw_ids=["m1"],
            wiki_pages=[_wiki_page("topic:a", "bank1", source_ids=["m1"])],
        )

        llm = MagicMock()
        llm.complete = AsyncMock()
        engine = LintEngine(vs, ws, llm, detect_contradictions=True)
        await engine.run("bank1")

        llm.complete.assert_not_awaited()
