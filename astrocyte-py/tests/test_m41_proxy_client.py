"""M4.1 — HTTP proxy recall client (TDD)."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

import pytest

from astrocyte.config import SourceConfig
from astrocyte.recall.proxy import fetch_proxy_recall_hits, gather_proxy_hits_for_bank


@pytest.mark.asyncio
async def test_fetch_proxy_parses_hits_json():
    cfg = SourceConfig(
        type="proxy",
        target_bank="b1",
        url="https://example.com/r?q={query}",
    )
    payload = {"hits": [{"text": "remote fact", "score": 0.88, "memory_id": "r1"}]}
    fake_gai = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
    ]

    with patch("astrocyte.recall.proxy.socket.getaddrinfo", return_value=fake_gai):
        with patch("astrocyte.recall.proxy.httpx.AsyncClient") as client_cls:
            instance = AsyncMock()
            resp = AsyncMock()
            resp.status_code = 200
            resp.raise_for_status = lambda: None
            resp.json = lambda: payload
            instance.request = AsyncMock(return_value=resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            client_cls.return_value = instance

            hits = await fetch_proxy_recall_hits("src1", cfg, query="hello", bank_id="b1")

    assert len(hits) == 1
    assert hits[0].text == "remote fact"
    assert hits[0].score == 0.88
    assert hits[0].memory_id == "r1"
    assert hits[0].source == "proxy:src1"


@pytest.mark.asyncio
async def test_gather_skips_non_matching_bank():
    config = type("C", (), {})()
    config.sources = {
        "p1": SourceConfig(type="proxy", target_bank="other", url="http://x?q={query}"),
    }
    with patch("astrocyte.recall.proxy.fetch_proxy_recall_hits", new_callable=AsyncMock) as fetch:
        out = await gather_proxy_hits_for_bank(config, query="q", bank_id="b1")
        assert out == []
        fetch.assert_not_called()
