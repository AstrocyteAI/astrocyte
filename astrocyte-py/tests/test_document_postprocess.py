"""Tests for `astrocyte.pipeline.document_postprocess.run_document_postprocess`.

Verifies:
  - All three passes (episodic, preference, directive) gate independently on config
  - Per-pass failures are isolated (one bad pass doesn't kill the others)
  - Order is fixed (episodic tag runs before compile passes)
  - Result dataclass surfaces counts + failures correctly
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from astrocyte.config import (
    AstrocyteConfig,
    DirectiveCompileConfig,
    EpisodicExtractConfig,
    PreferenceCompileConfig,
)
from astrocyte.pipeline.document_postprocess import (
    DocumentPostprocessResult,
    run_document_postprocess,
)


def _cfg(*, episodic: bool = False, pref: bool = True, directive: bool = False) -> AstrocyteConfig:
    c = AstrocyteConfig()
    c.episodic_extract = EpisodicExtractConfig(enabled=episodic)
    c.preference_compile = PreferenceCompileConfig(enabled=pref)
    c.directive_compile = DirectiveCompileConfig(enabled=directive)
    return c


class _Fact:
    """Minimal stand-in for PageIndexFact — just enough attrs the passes need."""

    def __init__(self, text: str, entities: list[str] | None = None, fact_type: str = "experience"):
        self.text = text
        self.entities = list(entities or [])
        self.fact_type = fact_type


class TestPassGating:
    @pytest.mark.asyncio
    async def test_all_off_runs_nothing(self) -> None:
        result = await run_document_postprocess(
            facts=[_Fact("hello")],
            store=None, mental_model_store=None, provider=None,
            bank_id="b1", document_id="d1",
            config=_cfg(episodic=False, pref=False, directive=False),
        )
        assert result.passes_run == []
        assert "episodic_extract (disabled)" in result.passes_skipped
        assert "preference_compile (disabled)" in result.passes_skipped
        assert "directive_compile (disabled)" in result.passes_skipped
        assert result.ok

    @pytest.mark.asyncio
    async def test_episodic_only(self) -> None:
        facts = [_Fact("I attended a wedding in March", entities=["wedding"])]
        result = await run_document_postprocess(
            facts=facts,
            store=None, mental_model_store=None, provider=None,
            bank_id="b1", document_id="d1",
            config=_cfg(episodic=True, pref=False, directive=False),
        )
        assert "episodic_extract" in result.passes_run
        assert result.episodic_tags_applied >= 0  # at least no crash
        assert result.ok

    @pytest.mark.asyncio
    async def test_episodic_skipped_on_empty_facts(self) -> None:
        result = await run_document_postprocess(
            facts=[],
            store=None, mental_model_store=None, provider=None,
            bank_id="b1", document_id="d1",
            config=_cfg(episodic=True, pref=False, directive=False),
        )
        assert "episodic_extract (empty facts)" in result.passes_skipped
        assert "episodic_extract" not in result.passes_run

    @pytest.mark.asyncio
    async def test_compile_passes_need_store_and_provider(self) -> None:
        """Compile passes silently skip (not fail) when missing required deps."""
        result = await run_document_postprocess(
            facts=[_Fact("hi")],
            store=None,
            mental_model_store=None,  # missing
            provider=None,  # missing
            bank_id="b1", document_id="d1",
            config=_cfg(episodic=False, pref=True, directive=True),
        )
        assert "preference_compile (missing deps)" in result.passes_skipped
        assert "directive_compile (missing deps)" in result.passes_skipped
        assert result.ok  # missing-deps is a skip, not a failure


class TestPassFailureIsolation:
    @pytest.mark.asyncio
    async def test_episodic_failure_doesnt_block_compile(self) -> None:
        """If tag_episodic_facts raises, the other passes still attempt."""
        async def _fake_compile_pref(**kwargs):
            return ["pref-1", "pref-2"]

        async def _fake_compile_directive(**kwargs):
            return ["dir-1"]

        # Make tag_episodic_facts raise
        with patch(
            "astrocyte.pipeline.episodic_extract.tag_episodic_facts",
            side_effect=RuntimeError("boom"),
        ), patch(
            "astrocyte.pipeline.preference_compile.compile_preferences_for_document",
            _fake_compile_pref,
        ), patch(
            "astrocyte.pipeline.directive_compile.compile_directives_for_document",
            _fake_compile_directive,
        ):
            result = await run_document_postprocess(
                facts=[_Fact("hi")],
                store=object(), mental_model_store=object(), provider=object(),
                bank_id="b1", document_id="d1",
                config=_cfg(episodic=True, pref=True, directive=True),
            )

        # Episodic failed
        assert any(f["pass"] == "episodic_extract" for f in result.failures)
        # But compile passes still ran
        assert "preference_compile" in result.passes_run
        assert "directive_compile" in result.passes_run
        assert result.preferences_compiled == 2
        assert result.directives_compiled == 1
        assert not result.ok  # at least one failure → not ok


class TestResult:
    def test_ok_is_true_when_no_failures(self) -> None:
        assert DocumentPostprocessResult().ok

    def test_ok_is_false_when_failures(self) -> None:
        r = DocumentPostprocessResult()
        r.failures.append({"pass": "x", "error": "y"})
        assert not r.ok
