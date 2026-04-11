"""Proxy recall URL validation (SSRF mitigation)."""

from __future__ import annotations

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
    from unittest.mock import patch

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
