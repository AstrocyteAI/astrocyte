"""M4.1 — Astrocyte.recall with configured proxy source (integration-style)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig, SourceConfig
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import VectorItem


@pytest.mark.asyncio
async def test_brain_recall_merges_configured_proxy_hits():
    cfg = AstrocyteConfig()
    cfg.barriers.pii.mode = "disabled"
    cfg.sources = {
        "remote": SourceConfig(
            type="proxy",
            target_bank="b1",
            url="https://example.com/r?q={query}",
        ),
    }

    vs = InMemoryVectorStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(
        vector_store=vs,
        llm_provider=llm,
        chunk_strategy="sentence",
        max_chunk_size=512,
        semantic_overfetch=2,
    )
    emb = [0.15] * 128
    await vs.store_vectors(
        [
            VectorItem(
                id="loc",
                bank_id="b1",
                vector=emb,
                text="local vector memory",
            ),
        ],
    )

    remote = {"hits": [{"text": "from remote API", "score": 0.92, "memory_id": "r99"}]}

    brain = Astrocyte(cfg)
    brain.set_pipeline(pipeline)

    with patch("astrocyte.recall.proxy.httpx.AsyncClient") as client_cls:
        instance = AsyncMock()
        resp = AsyncMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        resp.json = lambda: remote
        instance.get = AsyncMock(return_value=resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value = instance

        result = await brain.recall("find stuff", bank_id="b1", max_results=10)

    texts = {h.text for h in result.hits}
    assert "from remote API" in texts
    instance.get.assert_called_once()
