"""OAuth2 refresh rotation, HTTP Basic at token endpoint, authorization code exchange."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrocyte.config import SourceConfig
from astrocyte.recall.oauth import (
    clear_oauth2_token_cache_for_tests,
    exchange_oauth2_authorization_code,
    fetch_oauth2_client_credentials_token,
    fetch_oauth2_refresh_access_token,
    post_oauth2_token_endpoint,
)
from astrocyte.recall.proxy import auth_with_oauth_cache_namespace, build_proxy_headers, fetch_proxy_recall_hits


@pytest.fixture(autouse=True)
def _clear_oauth() -> None:
    clear_oauth2_token_cache_for_tests()
    yield
    clear_oauth2_token_cache_for_tests()


@pytest.mark.asyncio
async def test_post_token_endpoint_uses_basic_auth_and_strips_body_secrets() -> None:
    with patch("astrocyte.recall.oauth.httpx.AsyncClient") as client_cls:
        instance = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"access_token": "at", "expires_in": 60})
        instance.post = AsyncMock(return_value=resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value = instance

        await post_oauth2_token_endpoint(
            "https://id.example.com/token",
            {"grant_type": "client_credentials"},
            client_id="cid",
            client_secret="sec",
            token_endpoint_auth_method="client_secret_basic",
        )

    call_kw = instance.post.call_args
    assert "Basic " in call_kw[1]["headers"]["Authorization"]
    body = call_kw[1]["data"]
    assert "client_secret" not in body
    assert "client_id" not in body
    assert body["grant_type"] == "client_credentials"


@pytest.mark.asyncio
async def test_refresh_token_rotation_updates_stored_refresh() -> None:
    auth = {
        "token_url": "https://id.example.com/token",
        "client_id": "c",
        "client_secret": "s",
        "refresh_token": "refresh-v1",
        "_oauth_cache_id": "proxy-src-1",
    }

    with (
        patch(
            "astrocyte.recall.oauth.time.monotonic",
            side_effect=[100.0, 5000.0],
        ),
        patch("astrocyte.recall.oauth.post_oauth2_token_endpoint", new_callable=AsyncMock) as post,
    ):
        post.side_effect = [
            {
                "access_token": "a1",
                "expires_in": 3600,
                "refresh_token": "refresh-v2",
            },
            {
                "access_token": "a2",
                "expires_in": 3600,
            },
        ]

        t1 = await fetch_oauth2_refresh_access_token(auth)
        t2 = await fetch_oauth2_refresh_access_token(auth)

        assert t1 == "a1"
        assert t2 == "a2"
        assert post.call_count == 2

    second_call_data = post.call_args_list[1][0][1]
    assert second_call_data["refresh_token"] == "refresh-v2"


@pytest.mark.asyncio
async def test_exchange_authorization_code() -> None:
    with patch("astrocyte.recall.oauth.post_oauth2_token_endpoint", new_callable=AsyncMock) as post:
        post.return_value = {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        out = await exchange_oauth2_authorization_code(
            token_url="https://id.example.com/token",
            client_id="c",
            client_secret="s",
            code="auth-code-xyz",
            redirect_uri="https://app.example.com/cb",
            token_endpoint_auth_method="client_secret_post",
        )
        assert out["refresh_token"] == "rt"
        assert post.call_args[0][1]["grant_type"] == "authorization_code"
        assert post.call_args[0][1]["code"] == "auth-code-xyz"
        assert post.call_args[0][1]["redirect_uri"] == "https://app.example.com/cb"


def test_auth_with_oauth_cache_namespace_adds_id() -> None:
    out = auth_with_oauth_cache_namespace({"type": "bearer", "token": "t"}, "source-a")
    assert out is not None
    assert out["_oauth_cache_id"] == "source-a"
    assert out["token"] == "t"


@pytest.mark.asyncio
async def test_build_proxy_headers_oauth2_refresh_type() -> None:
    auth = {
        "type": "oauth2_refresh",
        "token_url": "https://id.example.com/token",
        "client_id": "c",
        "client_secret": "s",
        "refresh_token": "rt0",
    }
    with patch(
        "astrocyte.recall.oauth.fetch_oauth2_refresh_access_token",
        new_callable=AsyncMock,
        return_value="access-xyz",
    ):
        h = await build_proxy_headers(auth)
    assert h["Authorization"] == "Bearer access-xyz"


@pytest.mark.asyncio
async def test_build_proxy_headers_oauth2_grant_type_refresh_token_alias() -> None:
    auth = {
        "type": "oauth2",
        "grant_type": "refresh_token",
        "token_url": "https://id.example.com/token",
        "client_id": "c",
        "client_secret": "s",
        "refresh_token": "rt0",
    }
    with patch(
        "astrocyte.recall.oauth.fetch_oauth2_refresh_access_token",
        new_callable=AsyncMock,
        return_value="access-alias",
    ):
        h = await build_proxy_headers(auth)
    assert h["Authorization"] == "Bearer access-alias"


@pytest.mark.asyncio
async def test_fetch_oauth2_client_credentials_uses_basic_via_auth_config() -> None:
    auth = {
        "token_url": "https://id.example.com/token",
        "client_id": "cid",
        "client_secret": "sec",
        "token_endpoint_auth_method": "client_secret_basic",
        "_oauth_cache_id": "p1",
    }
    with patch("astrocyte.recall.oauth.httpx.AsyncClient") as client_cls:
        instance = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"access_token": "at", "expires_in": 3600})
        instance.post = AsyncMock(return_value=resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value = instance

        tok = await fetch_oauth2_client_credentials_token(auth)

    assert tok == "at"
    body = instance.post.call_args[1]["data"]
    assert "client_secret" not in body


@pytest.mark.asyncio
async def test_fetch_proxy_recall_injects_oauth_cache_namespace_for_source() -> None:
    """Proxy recall attaches ``_oauth_cache_id`` so OAuth caches are per ``source_id`` (ADR-003)."""
    cfg = SourceConfig(
        type="proxy",
        target_bank="b1",
        url="https://api.example.com/search?q={query}",
        auth={
            "type": "oauth2_client_credentials",
            "token_url": "https://id.example.com/token",
            "client_id": "c",
            "client_secret": "s",
        },
    )
    fake_gai = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
    ]
    with (
        patch(
            "astrocyte.recall.proxy.build_proxy_headers",
            new_callable=AsyncMock,
            return_value={"Authorization": "Bearer t"},
        ) as bh,
        patch("astrocyte.recall.proxy.socket.getaddrinfo", return_value=fake_gai),
        patch("astrocyte.recall.proxy.httpx.AsyncClient") as client_cls,
    ):
        instance = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"hits": []})
        instance.request = AsyncMock(return_value=resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value = instance

        await fetch_proxy_recall_hits("remote-kb", cfg, query="q", bank_id="b1")

    passed = bh.call_args[0][0]
    assert passed is not None
    assert passed["_oauth_cache_id"] == "remote-kb"
