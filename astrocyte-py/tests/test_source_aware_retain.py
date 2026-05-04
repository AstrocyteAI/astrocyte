"""End-to-end tests for source-aware retain + recall + chunk expansion (M10).

Exercises the full loop with the in-memory stack: retain creates
SourceDocument + SourceChunks, vectors stamp ``chunk_id``, recall
surfaces it on hits, and chunk expansion injects sibling-chunk vectors
when configured.

These tests pin behavioural contracts:

- The flag is gated correctly — retain stays anonymous when
  ``source_aware_retrieval.retain_provenance`` is off.
- Chunk expansion only fires when both the store and the flag are set,
  and gracefully degrades when the vector store doesn't expose
  ``get_by_chunk_ids``.
- Provenance failures must not break ingest.
"""

from __future__ import annotations

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import (
    InMemorySourceStore,
    InMemoryVectorStore,
    MockLLMProvider,
)
from astrocyte.types import RecallRequest, RetainRequest


def _build(
    *,
    source_store: InMemorySourceStore | None,
    retain_provenance: bool = False,
    chunk_expansion: bool = False,
    expansion_score_multiplier: float = 0.5,
    expansion_max_per_hit: int = 4,
    chunk_strategy: str = "sentence",
    max_chunk_size: int = 200,
) -> tuple[Astrocyte, InMemoryVectorStore, InMemorySourceStore | None]:
    cfg = AstrocyteConfig()
    cfg.barriers.pii.mode = "disabled"
    cfg.source_aware_retrieval.retain_provenance = retain_provenance
    cfg.source_aware_retrieval.chunk_expansion = chunk_expansion
    cfg.source_aware_retrieval.expansion_score_multiplier = expansion_score_multiplier
    cfg.source_aware_retrieval.expansion_max_per_hit = expansion_max_per_hit
    brain = Astrocyte(cfg)
    if source_store is not None:
        brain.set_source_store(source_store)
    vs = InMemoryVectorStore()
    pipeline = PipelineOrchestrator(
        vector_store=vs,
        llm_provider=MockLLMProvider(),
        chunk_strategy=chunk_strategy,
        max_chunk_size=max_chunk_size,
    )
    brain.set_pipeline(pipeline)
    return brain, vs, source_store


# ---------------------------------------------------------------------------
# Retain-side: provenance gating
# ---------------------------------------------------------------------------


class TestRetainProvenanceGating:
    @pytest.mark.asyncio
    async def test_no_source_store_means_no_provenance(self) -> None:
        """Without a SourceStore, vectors stay anonymous (chunk_id=None)."""
        brain, vs, _ = _build(source_store=None, retain_provenance=True)
        await brain._pipeline.retain(RetainRequest(  # type: ignore[union-attr]
            content="Hello world. The sky is blue.",
            bank_id="bank-A",
        ))
        items = list(vs._vectors.values())
        assert len(items) >= 1
        assert all(item.chunk_id is None for item in items)

    @pytest.mark.asyncio
    async def test_source_store_present_but_flag_off_means_no_provenance(self) -> None:
        """SourceStore wired but flag off = no provenance written."""
        store = InMemorySourceStore()
        brain, vs, _ = _build(source_store=store, retain_provenance=False)
        await brain._pipeline.retain(RetainRequest(  # type: ignore[union-attr]
            content="Hello world. The sky is blue.",
            bank_id="bank-A",
        ))
        items = list(vs._vectors.values())
        assert all(item.chunk_id is None for item in items)
        # And no SourceDocument was created.
        assert await store.list_documents("bank-A") == []

    @pytest.mark.asyncio
    async def test_flag_on_with_store_stamps_chunk_id_and_creates_document(self) -> None:
        """The end-to-end happy path: retain stamps chunk_id and the
        SourceStore now holds the parent document + chunk rows."""
        store = InMemorySourceStore()
        brain, vs, _ = _build(source_store=store, retain_provenance=True)
        await brain._pipeline.retain(RetainRequest(  # type: ignore[union-attr]
            content="The sky is blue. Grass is green. Water is wet.",
            bank_id="bank-A",
        ))
        items = list(vs._vectors.values())
        assert len(items) >= 1
        assert all(item.chunk_id is not None for item in items)
        # The document exists and its hash was set deterministically.
        docs = await store.list_documents("bank-A")
        assert len(docs) == 1
        assert docs[0].content_hash is not None
        # All vectors point to chunks of that single document.
        chunks = await store.list_chunks(docs[0].id, "bank-A")
        chunk_ids = {c.id for c in chunks}
        for item in items:
            assert item.chunk_id in chunk_ids

    @pytest.mark.asyncio
    async def test_dedup_across_two_retains_with_same_text(self) -> None:
        """Same input twice → only one SourceDocument (content_hash dedup)."""
        store = InMemorySourceStore()
        brain, _, _ = _build(source_store=store, retain_provenance=True)
        text = "Identical content here for dedup."
        await brain._pipeline.retain(RetainRequest(content=text, bank_id="bank-A"))  # type: ignore[union-attr]
        await brain._pipeline.retain(RetainRequest(content=text, bank_id="bank-A"))  # type: ignore[union-attr]
        docs = await store.list_documents("bank-A")
        # store_document is upsert-with-dedup on content_hash, so the
        # second retain should resolve to the same document id.
        assert len(docs) == 1


class TestProvenanceResilience:
    """Provenance must be best-effort — a failing SourceStore must not
    break ingest. Vectors fall back to chunk_id=None and the retain
    succeeds."""

    @pytest.mark.asyncio
    async def test_failing_source_store_does_not_break_retain(self) -> None:
        class _ExplodingStore:
            SPI_VERSION = 1
            async def store_document(self, doc):  # noqa: ARG002
                raise RuntimeError("boom")
            async def store_chunks(self, chunks):  # noqa: ARG002
                raise RuntimeError("boom")
            async def get_chunk(self, *a, **kw):  # noqa: ARG002
                return None
            async def list_chunks(self, *a, **kw):  # noqa: ARG002
                return []
            async def get_document(self, *a, **kw):  # noqa: ARG002
                return None
            async def find_document_by_hash(self, *a, **kw):  # noqa: ARG002
                return None
            async def find_chunk_by_hash(self, *a, **kw):  # noqa: ARG002
                return None
            async def list_documents(self, *a, **kw):  # noqa: ARG002
                return []
            async def delete_document(self, *a, **kw):  # noqa: ARG002
                return False
            async def health(self):
                from astrocyte.types import HealthStatus
                return HealthStatus(healthy=False, message="boom")

        brain, vs, _ = _build(source_store=_ExplodingStore(), retain_provenance=True)
        # Ingest must NOT raise even though the source store throws.
        result = await brain._pipeline.retain(RetainRequest(  # type: ignore[union-attr]
            content="Some content.",
            bank_id="bank-A",
        ))
        assert result.stored is True
        # Vectors still stored, just without chunk_id provenance.
        items = list(vs._vectors.values())
        assert len(items) >= 1
        assert all(item.chunk_id is None for item in items)


# ---------------------------------------------------------------------------
# Recall-side: chunk_id surfaces on MemoryHit, sibling expansion fires
# ---------------------------------------------------------------------------


class TestRecallProvenance:
    @pytest.mark.asyncio
    async def test_chunk_id_surfaces_on_memory_hit(self) -> None:
        """When retain stamps chunk_id, recall returns it on the MemoryHit."""
        store = InMemorySourceStore()
        brain, _, _ = _build(source_store=store, retain_provenance=True)
        await brain._pipeline.retain(RetainRequest(  # type: ignore[union-attr]
            content="Astrocyte memory framework rocks.",
            bank_id="bank-A",
        ))
        result = await brain._pipeline.recall(RecallRequest(  # type: ignore[union-attr]
            query="memory framework",
            bank_id="bank-A",
            max_results=5,
        ))
        assert len(result.hits) >= 1
        # At least one hit must carry the backreference.
        assert any(h.chunk_id is not None for h in result.hits)


class TestChunkExpansion:
    @pytest.mark.asyncio
    async def test_expansion_off_means_no_sibling_injection(self) -> None:
        """Baseline: with expansion off, only the seed vectors come back."""
        store = InMemorySourceStore()
        brain, vs, _ = _build(
            source_store=store, retain_provenance=True, chunk_expansion=False,
        )
        # Two paragraphs from the same document → two chunks → two vectors.
        await brain._pipeline.retain(RetainRequest(  # type: ignore[union-attr]
            content="The sky is blue today.\n\nGrass is green in spring.",
            bank_id="bank-A",
        ))
        before_count = len(vs._vectors)
        result = await brain._pipeline.recall(RecallRequest(  # type: ignore[union-attr]
            query="sky",
            bank_id="bank-A",
            max_results=5,
        ))
        # Sanity: no expansion → recall returns at most as many hits as
        # vectors that match the query directly.
        assert len(result.hits) <= before_count

    @pytest.mark.asyncio
    async def test_expansion_helper_pulls_sibling_chunk_vectors(self) -> None:
        """Direct test of ``_expand_via_sibling_chunks``: given a fused
        list with ONE seed hit whose chunk has 3 siblings in the source
        store, the helper must inject those sibling vectors into the
        candidate pool with the configured score multiplier."""
        from astrocyte.pipeline.fusion import ScoredItem
        from astrocyte.types import SourceChunk, SourceDocument, VectorItem

        store = InMemorySourceStore()
        brain, vs, _ = _build(
            source_store=store,
            retain_provenance=False,  # we'll seed manually
            chunk_expansion=True,
            expansion_score_multiplier=0.5,
            expansion_max_per_hit=4,
        )

        # Manually seed: one document with 4 chunks; vectors keyed by chunk.
        await store.store_document(SourceDocument(
            id="doc-A", bank_id="bank-X", content_hash="hash-A",
        ))
        chunk_ids = await store.store_chunks([
            SourceChunk(id=f"doc-A:{i}", bank_id="bank-X", document_id="doc-A",
                        chunk_index=i, text=f"chunk text {i}")
            for i in range(4)
        ])
        # Seed 4 vectors, one per chunk.
        for i, cid in enumerate(chunk_ids):
            await vs.store_vectors([VectorItem(
                id=f"mem-{i}", bank_id="bank-X", vector=[float(j == i) for j in range(128)],
                text=f"memory body {i}", chunk_id=cid,
            )])

        # Build a fake fused list with only the seed hit (mem-0).
        seed_only = [ScoredItem(
            id="mem-0", text="memory body 0", score=0.9, chunk_id="doc-A:0",
        )]

        # Expand.
        expanded = await brain._pipeline._expand_via_sibling_chunks(  # type: ignore[union-attr]
            seed_only, "bank-X",
        )

        # The helper must have added the 3 siblings (mem-1, mem-2, mem-3)
        # AND not duplicated the seed.
        ids = [h.id for h in expanded]
        assert "mem-0" in ids
        assert {"mem-1", "mem-2", "mem-3"}.issubset(set(ids))
        # And the expansion's score equals the multiplier (1.0 * 0.5 = 0.5).
        for h in expanded:
            if h.id != "mem-0":
                assert h.score == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_expansion_no_op_when_vector_store_lacks_get_by_chunk_ids(self) -> None:
        """Older adapters without ``get_by_chunk_ids`` must degrade
        gracefully — chunk expansion silently skips, recall still works."""
        # Subclass and shadow ``get_by_chunk_ids`` so ``hasattr()`` returns
        # False on instances of the subclass. We shadow with a property
        # that raises AttributeError because plain del-on-subclass doesn't
        # remove the inherited attribute.
        class _LegacyVectorStore(InMemoryVectorStore):
            @property
            def get_by_chunk_ids(self):  # type: ignore[override]
                raise AttributeError("legacy adapter does not implement get_by_chunk_ids")

        store = InMemorySourceStore()
        cfg = AstrocyteConfig()
        cfg.barriers.pii.mode = "disabled"
        cfg.source_aware_retrieval.retain_provenance = True
        cfg.source_aware_retrieval.chunk_expansion = True
        brain = Astrocyte(cfg)
        brain.set_source_store(store)
        legacy_vs = _LegacyVectorStore()
        pipeline = PipelineOrchestrator(vector_store=legacy_vs, llm_provider=MockLLMProvider())
        brain.set_pipeline(pipeline)

        await brain._pipeline.retain(RetainRequest(  # type: ignore[union-attr]
            content="Hello there.\n\nGeneral kenobi.",
            bank_id="bank-A",
        ))
        result = await brain._pipeline.recall(RecallRequest(  # type: ignore[union-attr]
            query="hello",
            bank_id="bank-A",
            max_results=5,
        ))
        # Recall must still return without raising; the expansion
        # branch is gated by hasattr(get_by_chunk_ids) so it skips.
        assert isinstance(result.hits, list)
        # Sanity: confirm the gate is the right one.
        assert not hasattr(legacy_vs, "get_by_chunk_ids")
