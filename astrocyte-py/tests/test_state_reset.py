"""Unit tests for the benchmark state-reset helper."""

from __future__ import annotations

import pytest

from astrocyte.eval._state_reset import _BENCH_TABLES, reset_benchmark_state


class TestNoOpPaths:
    async def test_no_dsn_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When DATABASE_URL is unset, helper returns silently without error."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        # Should not raise even though no real connection is attempted.
        await reset_benchmark_state(dsn=None)

    async def test_explicit_empty_dsn_is_noop(self) -> None:
        """Empty-string DSN is treated like missing."""
        await reset_benchmark_state(dsn="")


class TestTableList:
    """Guard the canonical truncation list so silent regressions are caught."""

    def test_includes_core_vector_table(self) -> None:
        assert "astrocyte_vectors" in _BENCH_TABLES

    def test_includes_wiki_layer(self) -> None:
        for t in (
            "astrocyte_wiki_pages",
            "astrocyte_wiki_revisions",
            "astrocyte_wiki_revision_sources",
            "astrocyte_wiki_links",
            "astrocyte_wiki_lint_issues",
        ):
            assert t in _BENCH_TABLES, f"missing {t}"

    def test_includes_entity_layer(self) -> None:
        for t in (
            "astrocyte_entities",
            "astrocyte_entity_aliases",
            "astrocyte_entity_links",
            "astrocyte_memory_entities",
            "astrocyte_age_mem_entity",
        ):
            assert t in _BENCH_TABLES, f"missing {t}"

    def test_includes_temporal_facts(self) -> None:
        assert "astrocyte_temporal_facts" in _BENCH_TABLES

    def test_includes_pgqueuer(self) -> None:
        assert "pgqueuer" in _BENCH_TABLES

    def test_no_duplicates(self) -> None:
        assert len(_BENCH_TABLES) == len(set(_BENCH_TABLES))


class TestExtraTables:
    async def test_extra_tables_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Caller-provided extra_tables don't cause a TypeError when DSN is unset."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        # Iterable accepted; no-op path doesn't actually try them.
        await reset_benchmark_state(extra_tables=["my_extension_table"])
