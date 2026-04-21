"""Orchestrator retain-side document-store mirroring (Session 1 Item 2).

Pins the contract that every chunk stored in the vector store is ALSO
stored in the document store when one is configured. This unblocks BM25
keyword retrieval, which `parallel_retrieve` already supported on the
recall side — but the retain side was previously only writing to the
vector store, so document_store was always empty.

Root cause writeup: docs/_design/platform-positioning.md LongMemEval
root causes §2.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import (
    InMemoryDocumentStore,
    InMemoryVectorStore,
    MockLLMProvider,
)
from astrocyte.types import RetainRequest


def _orch(with_doc_store: bool) -> tuple[PipelineOrchestrator, InMemoryVectorStore, InMemoryDocumentStore | None]:
    vector = InMemoryVectorStore()
    doc = InMemoryDocumentStore() if with_doc_store else None
    llm = MockLLMProvider()
    return (
        PipelineOrchestrator(vector_store=vector, document_store=doc, llm_provider=llm),
        vector,
        doc,
    )


# ---------------------------------------------------------------------------
# Retain-side mirroring — the primary contract
# ---------------------------------------------------------------------------


class TestRetainMirrorsToDocumentStore:
    async def test_chunk_lands_in_both_stores(self) -> None:
        orch, vector, doc = _orch(with_doc_store=True)
        result = await orch.retain(RetainRequest(
            content="The quick brown fox jumps over the lazy dog.",
            bank_id="b1",
        ))
        assert result.stored

        # Vector store has at least one chunk.
        vec_hits = await vector.list_vectors("b1", limit=10)
        assert len(vec_hits) >= 1

        # Document store has the same ids (keyword strategy can find them).
        for v in vec_hits:
            assert doc is not None
            dhit = await doc.get_document(v.id, "b1")
            assert dhit is not None, f"document_store missing chunk {v.id}"
            assert dhit.text == v.text

    async def test_bank_isolation_preserved(self) -> None:
        """A chunk stored in bank A must not be visible under bank B in
        either store. Bank isolation is a load-bearing invariant across
        multi-tenant deployments."""
        orch, _, doc = _orch(with_doc_store=True)
        await orch.retain(RetainRequest(content="alpha", bank_id="a"))
        await orch.retain(RetainRequest(content="beta", bank_id="b"))

        a_hits = await doc.search_fulltext("alpha", "a", limit=10)
        b_hits = await doc.search_fulltext("alpha", "b", limit=10)
        assert any("alpha" in h.text for h in a_hits)
        # "alpha" must not leak into bank b.
        assert not any("alpha" in h.text for h in b_hits)

    async def test_absent_document_store_does_not_break_retain(self) -> None:
        """Pipelines configured without a document_store must still
        retain successfully — the mirror code path no-ops quietly."""
        orch, vector, _ = _orch(with_doc_store=False)
        result = await orch.retain(RetainRequest(content="x", bank_id="b1"))
        assert result.stored
        assert len(await vector.list_vectors("b1", limit=10)) >= 1

    async def test_document_store_failure_does_not_abort_retain(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If the document store raises, the retain path must still
        complete — the vector store write already happened. Retrieval
        degrades to semantic-only for that chunk, which is acceptable
        failure mode (noisy warning in logs for operator triage)."""
        import logging

        class FailingDocStore:
            SPI_VERSION = 1
            async def store_document(self, doc, bank_id):  # type: ignore[no-untyped-def]
                raise RuntimeError("disk full")
            async def search_fulltext(self, query, bank_id, limit=10, filters=None):  # type: ignore[no-untyped-def]
                return []
            async def get_document(self, document_id, bank_id):  # type: ignore[no-untyped-def]
                return None
            async def health(self):  # type: ignore[no-untyped-def]
                from astrocyte.types import HealthStatus
                return HealthStatus(healthy=False, message="test failure")

        vector = InMemoryVectorStore()
        orch = PipelineOrchestrator(
            vector_store=vector,
            document_store=FailingDocStore(),  # type: ignore[arg-type]
            llm_provider=MockLLMProvider(),
        )
        with caplog.at_level(logging.WARNING, logger="astrocyte.mip"):
            result = await orch.retain(RetainRequest(content="x", bank_id="b1"))
        assert result.stored  # retain succeeded despite doc store failure
        assert any(
            "store_document failed" in r.getMessage() for r in caplog.records
        ), "operator needs a warning for triage"


# ---------------------------------------------------------------------------
# Recall-side integration — BM25 contributes to fusion
# ---------------------------------------------------------------------------


class TestKeywordRetrievalFires:
    """End-to-end: after retain, `recall` should surface the chunk via
    BM25 even when semantic similarity is weak. Uses InMemoryDocumentStore
    which is a basic substring/BM25-ish matcher, enough to verify the
    path participates in RRF."""

    async def test_keyword_match_surfaces_via_rrf(self) -> None:
        from astrocyte.types import RecallRequest

        orch, _, _ = _orch(with_doc_store=True)
        await orch.retain(RetainRequest(
            content="The quarterly review covers Q3 2024 revenue targets.",
            bank_id="b1",
        ))

        # Query that shares keywords with the chunk.
        result = await orch.recall(RecallRequest(
            query="Q3 2024 revenue", bank_id="b1", max_results=5,
        ))
        assert any("Q3 2024" in h.text for h in result.hits), (
            f"Expected keyword match in recall hits; got {[h.text for h in result.hits]}"
        )
