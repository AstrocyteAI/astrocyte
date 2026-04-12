"""M4.1 — Astrocyte.recall with configured proxy source (integration-style)."""

from __future__ import annotations

import ipaddress
from unittest.mock import AsyncMock, MagicMock, patch

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

    # Pin DNS to a deterministic RFC 5737 TEST-NET IP (documentation-only; no real DNS coupling).
    pinned_public = ipaddress.ip_address("203.0.113.10")
    with (
        patch(
            "astrocyte.recall.proxy._sync_dns_validate_and_first_public_ip",
            return_value=pinned_public,
        ),
        patch("astrocyte.recall.proxy.httpx.AsyncClient") as client_cls,
    ):
        instance = AsyncMock()
        # Implementation uses client.request(GET|POST, ...), not .get(); sync body methods avoid
        # AsyncMock coroutine warnings on raise_for_status() / json().
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=remote)
        instance.request = AsyncMock(return_value=resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value = instance

        result = await brain.recall("find stuff", bank_id="b1", max_results=10)

    texts = {h.text for h in result.hits}
    assert "from remote API" in texts
    assert "local vector memory" in texts
    instance.request.assert_called_once()
    args, kwargs = instance.request.call_args
    assert args[0] == "GET"
    assert "203.0.113.10" in args[1]
    params = kwargs.get("params")
    assert params is not None
    assert dict(params).get("q") == "find stuff"
    hdrs = kwargs.get("headers") or {}
    assert isinstance(hdrs, dict)
    assert hdrs.get("Host") == "example.com"
