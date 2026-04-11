"""Tests for design-level improvements: list_vectors, consolidation, clear_bank, SPI versioning."""

import math
from typing import ClassVar

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.pipeline.consolidation import run_consolidation
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.provider import _SUPPORTED_VERSIONS, check_spi_version
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import VectorItem


def _make_brain() -> tuple[Astrocyte, InMemoryVectorStore, MockLLMProvider]:
    config = AstrocyteConfig()
    config.provider_tier = "storage"
    config.barriers.pii.mode = "disabled"
    config.escalation.degraded_mode = "error"
    brain = Astrocyte(config)
    vs = InMemoryVectorStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=vs, llm_provider=llm)
    brain.set_pipeline(pipeline)
    return brain, vs, llm


def _make_vector(vid: str, bank_id: str, vector: list[float], text: str = "test") -> VectorItem:
    return VectorItem(id=vid, bank_id=bank_id, vector=vector, text=text)


def _normalized(raw: list[float]) -> list[float]:
    """Return a unit-length vector."""
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw] if norm > 0 else raw


# ---------------------------------------------------------------------------
# list_vectors pagination
# ---------------------------------------------------------------------------


class TestListVectorsPagination:
    async def test_list_vectors_returns_all(self):
        vs = InMemoryVectorStore()
        items = [_make_vector(f"v{i}", "bank-1", [float(i)]) for i in range(5)]
        await vs.store_vectors(items)

        result = await vs.list_vectors("bank-1")
        assert len(result) == 5

    async def test_list_vectors_pagination_offset_limit(self):
        vs = InMemoryVectorStore()
        items = [_make_vector(f"v{i:03d}", "bank-1", [float(i)]) for i in range(10)]
        await vs.store_vectors(items)

        page1 = await vs.list_vectors("bank-1", offset=0, limit=3)
        page2 = await vs.list_vectors("bank-1", offset=3, limit=3)
        page3 = await vs.list_vectors("bank-1", offset=6, limit=3)
        page4 = await vs.list_vectors("bank-1", offset=9, limit=3)

        assert len(page1) == 3
        assert len(page2) == 3
        assert len(page3) == 3
        assert len(page4) == 1

        all_ids = [v.id for v in page1 + page2 + page3 + page4]
        assert len(set(all_ids)) == 10  # no duplicates

    async def test_list_vectors_bank_isolation(self):
        vs = InMemoryVectorStore()
        await vs.store_vectors([_make_vector("v1", "bank-a", [1.0])])
        await vs.store_vectors([_make_vector("v2", "bank-b", [2.0])])

        result_a = await vs.list_vectors("bank-a")
        result_b = await vs.list_vectors("bank-b")
        assert len(result_a) == 1
        assert result_a[0].id == "v1"
        assert len(result_b) == 1
        assert result_b[0].id == "v2"

    async def test_list_vectors_empty_bank(self):
        vs = InMemoryVectorStore()
        result = await vs.list_vectors("nonexistent")
        assert result == []

    async def test_list_vectors_stable_order(self):
        vs = InMemoryVectorStore()
        # Insert in reverse order
        for i in reversed(range(5)):
            await vs.store_vectors([_make_vector(f"v{i:03d}", "bank-1", [float(i)])])

        result = await vs.list_vectors("bank-1")
        ids = [v.id for v in result]
        assert ids == sorted(ids), "list_vectors should return in stable sorted order"


# ---------------------------------------------------------------------------
# Consolidation dedup
# ---------------------------------------------------------------------------


class TestConsolidation:
    async def test_removes_near_duplicates(self):
        vs = InMemoryVectorStore()
        vec = _normalized([1.0, 0.0, 0.0])
        near_dup = _normalized([1.0, 0.001, 0.0])  # cosine sim ~0.9999
        different = _normalized([0.0, 1.0, 0.0])

        await vs.store_vectors(
            [
                _make_vector("v1", "bank-1", vec, "original"),
                _make_vector("v2", "bank-1", near_dup, "near duplicate"),
                _make_vector("v3", "bank-1", different, "different"),
            ]
        )

        result = await run_consolidation(vs, "bank-1", similarity_threshold=0.99)

        assert result.total_scanned == 3
        assert result.duplicates_removed == 1
        remaining = await vs.list_vectors("bank-1")
        remaining_ids = {v.id for v in remaining}
        assert "v1" in remaining_ids, "first occurrence should be kept"
        assert "v3" in remaining_ids, "different vector should be kept"
        assert "v2" not in remaining_ids, "near-duplicate should be removed"

    async def test_no_duplicates_removes_nothing(self):
        vs = InMemoryVectorStore()
        await vs.store_vectors(
            [
                _make_vector("v1", "bank-1", _normalized([1.0, 0.0, 0.0])),
                _make_vector("v2", "bank-1", _normalized([0.0, 1.0, 0.0])),
                _make_vector("v3", "bank-1", _normalized([0.0, 0.0, 1.0])),
            ]
        )

        result = await run_consolidation(vs, "bank-1", similarity_threshold=0.95)
        assert result.duplicates_removed == 0
        assert result.total_scanned == 3

    async def test_empty_bank(self):
        vs = InMemoryVectorStore()
        result = await run_consolidation(vs, "bank-1")
        assert result.duplicates_removed == 0
        assert result.total_scanned == 0

    async def test_skips_when_no_list_vectors(self):
        """VectorStore without list_vectors should be gracefully skipped."""

        class MinimalVectorStore:
            async def store_vectors(self, items):
                return [i.id for i in items]

            async def search_similar(self, *a, **kw):
                return []

            async def delete(self, ids, bank_id):
                return 0

        result = await run_consolidation(MinimalVectorStore(), "bank-1")  # type: ignore[arg-type]
        assert result.duplicates_removed == 0
        assert result.total_scanned == 0

    async def test_respects_threshold(self):
        vs = InMemoryVectorStore()
        # Two vectors with moderate similarity
        v1 = _normalized([1.0, 0.5, 0.0])
        v2 = _normalized([1.0, 0.6, 0.0])

        await vs.store_vectors(
            [
                _make_vector("v1", "bank-1", v1),
                _make_vector("v2", "bank-1", v2),
            ]
        )

        # With very high threshold, they shouldn't be considered duplicates
        result = await run_consolidation(vs, "bank-1", similarity_threshold=0.9999)
        assert result.duplicates_removed == 0

        # With lower threshold, they should be
        result = await run_consolidation(vs, "bank-1", similarity_threshold=0.90)
        assert result.duplicates_removed == 1


# ---------------------------------------------------------------------------
# clear_bank
# ---------------------------------------------------------------------------


class TestClearBank:
    async def test_clear_bank_deletes_all_vectors(self):
        brain, vs, llm = _make_brain()
        # Store several memories
        await brain.retain("Memory one", bank_id="bank-1")
        await brain.retain("Memory two", bank_id="bank-1")
        await brain.retain("Memory three", bank_id="bank-1")

        vectors_before = await vs.list_vectors("bank-1")
        assert len(vectors_before) >= 3

        result = await brain.clear_bank("bank-1")
        assert result.deleted_count >= 3

        vectors_after = await vs.list_vectors("bank-1")
        assert len(vectors_after) == 0

    async def test_clear_bank_does_not_affect_other_banks(self):
        brain, vs, llm = _make_brain()
        await brain.retain("Memory in bank-1", bank_id="bank-1")
        await brain.retain("Memory in bank-2", bank_id="bank-2")

        await brain.clear_bank("bank-1")

        assert len(await vs.list_vectors("bank-1")) == 0
        assert len(await vs.list_vectors("bank-2")) >= 1

    async def test_clear_bank_empty_bank(self):
        brain, vs, llm = _make_brain()
        result = await brain.clear_bank("empty-bank")
        assert result.deleted_count == 0

    async def test_forget_scope_all_same_as_clear_bank(self):
        brain, vs, llm = _make_brain()
        await brain.retain("Some data", bank_id="bank-1")

        result = await brain.forget("bank-1", scope="all")
        assert result.deleted_count >= 1
        assert len(await vs.list_vectors("bank-1")) == 0


# ---------------------------------------------------------------------------
# SPI version negotiation
# ---------------------------------------------------------------------------


class TestSPIVersionNegotiation:
    def test_accepts_v1(self):
        class MyProvider:
            SPI_VERSION: ClassVar[int] = 1

        version = check_spi_version(MyProvider(), "VectorStore")
        assert version == 1

    def test_defaults_to_v1_when_missing(self):
        class NoVersionProvider:
            pass

        version = check_spi_version(NoVersionProvider(), "VectorStore")
        assert version == 1

    def test_rejects_unsupported_version(self):
        class FutureProvider:
            SPI_VERSION: ClassVar[int] = 99

        from astrocyte.errors import ConfigError

        with pytest.raises(ConfigError, match="version 99 is not supported"):
            check_spi_version(FutureProvider(), "VectorStore")

    def test_rejects_for_all_protocol_names(self):
        """All known protocols should reject unsupported versions."""
        from astrocyte.errors import ConfigError

        class BadProvider:
            SPI_VERSION: ClassVar[int] = 42

        for protocol_name in _SUPPORTED_VERSIONS:
            with pytest.raises(ConfigError):
                check_spi_version(BadProvider(), protocol_name)

    def test_error_message_includes_supported_versions(self):
        class BadProvider:
            SPI_VERSION: ClassVar[int] = 5

        from astrocyte.errors import ConfigError

        with pytest.raises(ConfigError, match="Supported versions: \\[1\\]"):
            check_spi_version(BadProvider(), "EngineProvider")

    def test_unknown_protocol_defaults_to_v1_set(self):
        class V1Provider:
            SPI_VERSION: ClassVar[int] = 1

        # Unknown protocol name should still accept v1 (fallback)
        version = check_spi_version(V1Provider(), "UnknownProtocol")
        assert version == 1


# ---------------------------------------------------------------------------
# Integrations use public API
# ---------------------------------------------------------------------------


class TestIntegrationsUsePublicAPI:
    """Verify integrations call brain.clear_bank() not private _do_forget()."""

    def test_crewai_reset_uses_clear_bank(self):
        import inspect

        from astrocyte.integrations.crewai import AstrocyteCrewMemory

        source = inspect.getsource(AstrocyteCrewMemory.reset)
        assert "clear_bank" in source
        assert "_do_forget" not in source

    def test_llamaindex_reset_uses_clear_bank(self):
        import inspect

        from astrocyte.integrations.llamaindex import AstrocyteLlamaMemory

        source = inspect.getsource(AstrocyteLlamaMemory.reset)
        assert "clear_bank" in source
        assert "_do_forget" not in source

    def test_camel_ai_clear_uses_clear_bank(self):
        import inspect

        from astrocyte.integrations.camel_ai import AstrocyteCamelMemory

        source = inspect.getsource(AstrocyteCamelMemory.clear)
        assert "clear_bank" in source
        assert "_do_forget" not in source
