"""Proxy recall URL validation (SSRF mitigation)."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrocyte.config import SourceConfig
from astrocyte.recall.proxy import validate_proxy_recall_url


@pytest.mark.parametrize(
    ("url",),
    [
        ("http://127.0.0.1/api",),
        ("http://[::1]/api",),
        ("http://10.0.0.1/h",),
        ("http://192.168.0.1/h",),
        ("http://169.254.169.254/latest/meta-data",),
        ("http://0.0.0.0/",),
        ("ftp://example.com/x",),
        ("http://localhost/x",),
        ("",),
    ],
)
def test_validate_proxy_recall_url_rejects_unsafe(url: str) -> None:
    with pytest.raises(ValueError):
        validate_proxy_recall_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/search?q=1",
        "http://bad.test/h?q=%2F",  # reserved TLD; tests use mocks
    ],
)
def test_validate_proxy_recall_url_accepts_public_http_urls(url: str) -> None:
    validate_proxy_recall_url(url)


@pytest.mark.asyncio
async def test_fetch_proxy_recall_hits_skips_http_before_client() -> None:
    """Unsafe configured URL must fail validation before httpx runs."""
    from astrocyte.recall.proxy import fetch_proxy_recall_hits

    cfg = SourceConfig(
        type="proxy",
        target_bank="b1",
        url="http://127.0.0.1/r?q={query}",
    )
    with patch("astrocyte.recall.proxy.httpx.AsyncClient") as client_cls:
        with pytest.raises(ValueError, match="loopback|private|localhost"):
            await fetch_proxy_recall_hits("s1", cfg, query="x", bank_id="b1")
        client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_proxy_recall_hits_dns_rejects_resolved_private_address() -> None:
    """DNS must not resolve to RFC1918 / forbidden addresses before httpx runs."""
    from astrocyte.recall.proxy import fetch_proxy_recall_hits

    cfg = SourceConfig(
        type="proxy",
        target_bank="b1",
        url="https://example.com/r?q={query}",
    )
    fake_gai = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443)),
    ]
    with patch("astrocyte.recall.proxy.socket.getaddrinfo", return_value=fake_gai):
        with patch("astrocyte.recall.proxy.httpx.AsyncClient") as client_cls:
            with pytest.raises(ValueError, match="forbidden address"):
                await fetch_proxy_recall_hits("s1", cfg, query="x", bank_id="b1")
            client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_proxy_recall_hits_proceeds_after_public_dns() -> None:
    from astrocyte.recall.proxy import fetch_proxy_recall_hits

    cfg = SourceConfig(
        type="proxy",
        target_bank="b1",
        url="https://example.com/r?q={query}",
    )
    fake_gai = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
    ]
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"hits": []}
    mock_resp.raise_for_status = MagicMock()
    inner = AsyncMock()
    inner.request = AsyncMock(return_value=mock_resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch("astrocyte.recall.proxy.socket.getaddrinfo", return_value=fake_gai):
        with patch("astrocyte.recall.proxy.httpx.AsyncClient", return_value=cm):
            out = await fetch_proxy_recall_hits("s1", cfg, query="x", bank_id="b1")
    assert out == []
    inner.request.assert_called_once()
    call = inner.request.call_args
    assert call[0][0] == "GET"
    assert "93.184.216.34" in str(call[0][1])
    assert call[1]["headers"]["Host"] == "example.com"
    assert call[1]["extensions"] == {"sni_hostname": "example.com"}


@pytest.mark.asyncio
async def test_fetch_proxy_recall_hits_literal_ip_no_host_override() -> None:
    """Literal IP in URL uses the same URL for the request (no DNS-name Host header)."""
    from astrocyte.recall.proxy import fetch_proxy_recall_hits

    cfg = SourceConfig(
        type="proxy",
        target_bank="b1",
        url="https://93.184.216.34/r?q={query}",
    )
    fake_gai = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
    ]
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"hits": []}
    mock_resp.raise_for_status = MagicMock()
    inner = AsyncMock()
    inner.request = AsyncMock(return_value=mock_resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch("astrocyte.recall.proxy.socket.getaddrinfo", return_value=fake_gai):
        with patch("astrocyte.recall.proxy.httpx.AsyncClient", return_value=cm):
            await fetch_proxy_recall_hits("s1", cfg, query="x", bank_id="b1")
    call = inner.request.call_args
    assert "93.184.216.34" in str(call[0][1])
    assert call[1]["headers"].get("Host") is None
    assert call[1]["extensions"] is None
