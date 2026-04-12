"""M4.1 — OAuth2, Prometheus proxy metrics, TieredRetriever wiring on Astrocyte."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.recall.oauth import clear_oauth2_token_cache_for_tests
from astrocyte.recall.proxy import build_proxy_headers
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import RecallRequest, RecallResult


@pytest.fixture(autouse=True)
def _clear_oauth_cache():
    clear_oauth2_token_cache_for_tests()
    yield
    clear_oauth2_token_cache_for_tests()


@pytest.mark.asyncio
async def test_build_proxy_headers_oauth2():
    with patch(
        "astrocyte.recall.oauth.fetch_oauth2_client_credentials_token",
        new_callable=AsyncMock,
    ) as ft:
        ft.return_value = "access-token-xyz"
        headers = await build_proxy_headers(
            {
                "type": "oauth2_client_credentials",
                "token_url": "https://id.example.com/oauth/token",
                "client_id": "cid",
                "client_secret": "sec",
            }
        )
    assert headers["Authorization"] == "Bearer access-token-xyz"


@pytest.mark.asyncio
async def test_proxy_recall_records_metrics_on_error():
    from astrocyte.config import SourceConfig
    from astrocyte.policy.observability import MetricsCollector
    from astrocyte.recall.proxy import fetch_proxy_recall_hits

    m = MagicMock(spec=MetricsCollector)
    m.enabled = True
    cfg = SourceConfig(type="proxy", target_bank="b1", url="http://bad.test/h?q={query}")
    fake_gai = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80)),
    ]

    with patch("astrocyte.recall.proxy.socket.getaddrinfo", return_value=fake_gai):
        with patch("astrocyte.recall.proxy.httpx.AsyncClient") as client_cls:
            instance = AsyncMock()
            instance.request = AsyncMock(side_effect=OSError("network"))
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            client_cls.return_value = instance

            with pytest.raises(OSError):
                await fetch_proxy_recall_hits("s1", cfg, query="q", bank_id="b1", metrics=m)

    m.inc_counter.assert_called()
    args = m.inc_counter.call_args_list[-1][0]
    assert args[0] == "astrocyte_proxy_recall_total"
    assert args[1]["status"] == "error"


@pytest.mark.asyncio
async def test_tiered_retriever_wired_when_enabled():
    cfg = AstrocyteConfig()
    cfg.barriers.pii.mode = "disabled"
    cfg.tiered_retrieval.enabled = True
    cfg.tiered_retrieval.max_tier = 3
    cfg.recall_cache.enabled = True

    brain = Astrocyte(cfg)
    assert brain._tiered_retriever is None

    pipeline = PipelineOrchestrator(
        vector_store=InMemoryVectorStore(),
        llm_provider=MockLLMProvider(),
    )
    brain.set_pipeline(pipeline)

    assert brain._tiered_retriever is not None

    mock_result = RecallResult(hits=[], total_available=0, truncated=False)
    with patch.object(
        brain._tiered_retriever,
        "retrieve",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as tr:
        out = await brain._do_recall(RecallRequest(query="hello", bank_id="b1"))
        tr.assert_called_once()
        assert out is mock_result


@pytest.mark.asyncio
async def test_tiered_disabled_uses_pipeline_directly():
    cfg = AstrocyteConfig()
    cfg.barriers.pii.mode = "disabled"
    cfg.tiered_retrieval.enabled = False

    brain = Astrocyte(cfg)
    pipeline = PipelineOrchestrator(
        vector_store=InMemoryVectorStore(),
        llm_provider=MockLLMProvider(),
    )
    brain.set_pipeline(pipeline)
    assert brain._tiered_retriever is None

    with patch.object(
        pipeline,
        "recall",
        new_callable=AsyncMock,
        return_value=RecallResult(hits=[], total_available=0, truncated=False),
    ) as pr:
        await brain._do_recall(RecallRequest(query="x", bank_id="b1"))
        pr.assert_called_once()
