"""M4.1 — POST body and extended auth for proxy recall."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

import pytest

from astrocyte.config import SourceConfig
from astrocyte.recall.proxy import PLACE_BANK, PLACE_QUERY, fetch_proxy_recall_hits


@pytest.mark.asyncio
async def test_fetch_proxy_post_default_json_body():
    cfg = SourceConfig(
        type="proxy",
        target_bank="b1",
        url="https://example.com/search",
        recall_method="POST",
    )
    payload = {"hits": [{"text": "post hit", "score": 0.9}]}
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

            hits = await fetch_proxy_recall_hits("p1", cfg, query="q1", bank_id="b1")

    assert len(hits) == 1
    assert hits[0].text == "post hit"
    instance.request.assert_called_once()
    call_kw = instance.request.call_args
    assert call_kw[0][0] == "POST"
    assert "93.184.216.34" in str(call_kw[0][1])
    assert call_kw[1]["json"] == {"query": "q1", "bank_id": "b1"}


@pytest.mark.asyncio
async def test_fetch_proxy_post_custom_body_dict():
    cfg = SourceConfig(
        type="proxy",
        target_bank="b1",
        url="https://example.com/v1/recall",
        recall_method="post",
        recall_body={
            "text": PLACE_QUERY,
            "namespace": PLACE_BANK,
            "top_k": 5,
        },
    )
    payload = {"results": [{"text": "custom", "score": 0.8}]}
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

            await fetch_proxy_recall_hits("p1", cfg, query="hello", bank_id="my-bank")

    body = instance.request.call_args[1]["json"]
    assert body == {"text": "hello", "namespace": "my-bank", "top_k": 5}


@pytest.mark.asyncio
async def test_fetch_proxy_api_key_and_extra_headers():
    cfg = SourceConfig(
        type="proxy",
        target_bank="b1",
        url="https://example.com/r?q={query}",
        auth={
            "type": "api_key",
            "header": "X-Custom-Key",
            "value": "secret123",
            "headers": {"X-Tenant": "acme"},
        },
    )
    payload = {"hits": []}
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

            await fetch_proxy_recall_hits("p1", cfg, query="z", bank_id="b1")

    headers = instance.request.call_args[1]["headers"]
    assert headers["X-Custom-Key"] == "secret123"
    assert headers["X-Tenant"] == "acme"
