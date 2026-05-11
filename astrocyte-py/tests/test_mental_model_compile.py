"""M11.2: mental-model compile pass unit tests."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrocyte.pipeline.mental_model_compile import (
    _format_sections_for_prompt,
    _slugify,
    compile_mental_models_for_document,
)
from astrocyte.testing.in_memory import (
    InMemoryMentalModelStore,
    InMemoryPageIndexStore,
)
from astrocyte.types import Completion, PageIndexDocument, PageIndexSection


def _section(line_num: int, summary: str, date_str: str = "2023-05-08") -> PageIndexSection:
    return PageIndexSection(
        document_id="doc-1",
        line_num=line_num,
        node_id=f"{line_num:04d}",
        title=f"node-{line_num}",
        summary=summary,
        session_date=datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc),
    )


class TestSlugify:
    def test_basic_lowercase_and_hyphens(self) -> None:
        assert _slugify("Photography Gear Preference") == "photography-gear-preference"

    def test_strips_punctuation(self) -> None:
        assert _slugify("User's Diet!?") == "users-diet"

    def test_empty_falls_back_to_model(self) -> None:
        assert _slugify("") == "model"
        assert _slugify("!!!") == "model"

    def test_truncates_long_input(self) -> None:
        # Inputs over 60 chars get truncated — the slugified prefix becomes
        # the model_id suffix, which has a length budget downstream.
        long_title = "this is a very long title " * 5
        assert len(_slugify(long_title)) <= 60


class TestFormatSections:
    def test_sorts_chronologically_by_line_num(self) -> None:
        sections = [_section(10, "third"), _section(1, "first"), _section(5, "second")]
        out = _format_sections_for_prompt(sections)
        # Each line has a [line=N ...] prefix; check order via line numbers
        lines = out.splitlines()
        assert "line=1" in lines[0]
        assert "line=5" in lines[1]
        assert "line=10" in lines[2]

    def test_omits_sections_without_summary(self) -> None:
        sections = [
            _section(1, "real summary"),
            PageIndexSection(
                document_id="doc-1", line_num=2, node_id="0002",
                title="", summary=None, session_date=None,
            ),
        ]
        out = _format_sections_for_prompt(sections)
        assert "line=1" in out
        assert "line=2" not in out

    def test_renders_no_date_for_missing_session_date(self) -> None:
        s = PageIndexSection(
            document_id="doc-1", line_num=1, node_id="0001",
            title="t", summary="s", session_date=None,
        )
        out = _format_sections_for_prompt([s])
        assert "date=no-date" in out

    def test_caps_at_max_sections(self) -> None:
        sections = [_section(i, f"sum-{i}") for i in range(100)]
        out = _format_sections_for_prompt(sections, max_sections=10)
        assert out.count("\n") == 9  # 10 lines


class TestCompileMentalModels:
    @pytest.fixture
    def stores(self) -> tuple[InMemoryPageIndexStore, InMemoryMentalModelStore]:
        return InMemoryPageIndexStore(), InMemoryMentalModelStore()

    @pytest.fixture
    async def populated_doc(self, stores) -> str:
        pi_store, _ = stores
        doc = PageIndexDocument(
            id="", bank_id="b1", source_id="s1", md_text="# md",
            reference_date=datetime(2023, 5, 8, tzinfo=timezone.utc),
            built_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
        )
        doc_id = await pi_store.save_document(doc)
        await pi_store.save_sections(doc_id, [
            _section(1, "User discussed Sony A7 III and lens preferences"),
            _section(5, "User mentioned weekly hip-hop classes at Street Beats"),
            _section(10, "User shared vegetarian recipes"),
        ])
        return doc_id

    async def test_happy_path_upserts_models(self, stores, populated_doc) -> None:
        pi_store, mm_store = stores
        doc_id = populated_doc

        provider = MagicMock()
        provider.complete = AsyncMock(return_value=Completion(
            text='{"models": ['
                 '{"title": "Camera preference", "content": "Sony A7 III"},'
                 '{"title": "Dance practice", "content": "Hip-hop at Street Beats"}'
                 ']}',
            model="gpt-4o-mini",
        ))

        ids = await compile_mental_models_for_document(
            page_index_store=pi_store,
            mental_model_store=mm_store,
            bank_id="b1",
            document_id=doc_id,
            provider=provider,
        )

        assert len(ids) == 2
        models = await mm_store.list("b1", scope=f"document:{doc_id}")
        assert {m.title for m in models} == {"Camera preference", "Dance practice"}
        # All carry doc-scoped scope
        assert all(m.scope == f"document:{doc_id}" for m in models)
        # Revision starts at 1 on first upsert
        assert all(m.revision == 1 for m in models)

    async def test_empty_models_list_returns_empty(self, stores, populated_doc) -> None:
        pi_store, mm_store = stores
        provider = MagicMock()
        provider.complete = AsyncMock(return_value=Completion(
            text='{"models": []}', model="gpt-4o-mini",
        ))
        ids = await compile_mental_models_for_document(
            page_index_store=pi_store,
            mental_model_store=mm_store,
            bank_id="b1",
            document_id=populated_doc,
            provider=provider,
        )
        assert ids == []

    async def test_json_parse_failure_returns_empty(self, stores, populated_doc) -> None:
        pi_store, mm_store = stores
        provider = MagicMock()
        provider.complete = AsyncMock(return_value=Completion(
            text="not valid json {{", model="gpt-4o-mini",
        ))
        ids = await compile_mental_models_for_document(
            page_index_store=pi_store,
            mental_model_store=mm_store,
            bank_id="b1",
            document_id=populated_doc,
            provider=provider,
        )
        assert ids == []

    async def test_missing_title_or_content_skipped(self, stores, populated_doc) -> None:
        pi_store, mm_store = stores
        provider = MagicMock()
        provider.complete = AsyncMock(return_value=Completion(
            text='{"models": ['
                 '{"title": "good", "content": "ok"},'
                 '{"title": "", "content": "missing title"},'
                 '{"title": "no content", "content": ""}'
                 ']}',
            model="gpt-4o-mini",
        ))
        ids = await compile_mental_models_for_document(
            page_index_store=pi_store,
            mental_model_store=mm_store,
            bank_id="b1",
            document_id=populated_doc,
            provider=provider,
        )
        assert len(ids) == 1

    async def test_no_sections_returns_empty(self, stores) -> None:
        pi_store, mm_store = stores
        ids = await compile_mental_models_for_document(
            page_index_store=pi_store,
            mental_model_store=mm_store,
            bank_id="b1",
            document_id="nonexistent-doc",
            provider=MagicMock(),
        )
        assert ids == []

    async def test_llm_failure_returns_empty(self, stores, populated_doc) -> None:
        pi_store, mm_store = stores
        provider = MagicMock()
        provider.complete = AsyncMock(side_effect=RuntimeError("api down"))
        ids = await compile_mental_models_for_document(
            page_index_store=pi_store,
            mental_model_store=mm_store,
            bank_id="b1",
            document_id=populated_doc,
            provider=provider,
        )
        assert ids == []

    async def test_idempotent_via_upsert_revision_bump(self, stores, populated_doc) -> None:
        """Second compile run bumps revisions instead of creating duplicates."""
        pi_store, mm_store = stores
        provider = MagicMock()
        provider.complete = AsyncMock(return_value=Completion(
            text='{"models": [{"title": "Diet", "content": "vegetarian"}]}',
            model="gpt-4o-mini",
        ))
        await compile_mental_models_for_document(
            page_index_store=pi_store,
            mental_model_store=mm_store,
            bank_id="b1",
            document_id=populated_doc,
            provider=provider,
        )
        await compile_mental_models_for_document(
            page_index_store=pi_store,
            mental_model_store=mm_store,
            bank_id="b1",
            document_id=populated_doc,
            provider=provider,
        )
        models = await mm_store.list("b1", scope=f"document:{populated_doc}")
        # Same model_id → upserted once with revision=2
        assert len(models) == 1
        assert models[0].revision == 2
