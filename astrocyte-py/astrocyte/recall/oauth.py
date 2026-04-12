"""OAuth2 for proxy recall — client credentials, refresh (with rotation), authorization code exchange."""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TOKEN_CACHE: dict[str, _TokenState] = {}


@dataclass
class _TokenState:
    access_token: str
    expires_at: float  # monotonic
    refresh_token: str | None = None  # updated when issuer rotates refresh tokens


def _client_credentials_cache_key(auth: dict[str, str | int | float | bool | None]) -> str:
    ns = auth.get("_oauth_cache_id")
    if ns is not None and str(ns).strip():
        return f"cc|{ns}"
    tid = str(auth.get("token_url") or "")
    cid = str(auth.get("client_id") or "")
    sc = str(auth.get("scope") or "")
    return f"{tid}|{cid}|{sc}"


def _refresh_cache_key(auth: dict[str, str | int | float | bool | None]) -> str:
    """Partition key for refresh-token cache entries.

    Requires ``_oauth_cache_id`` (proxy recall sets this via :func:`auth_with_oauth_cache_namespace`)
    so we never hash ``refresh_token`` for keying — avoids collisions across sources and satisfies
    static analysis that treats bearer secrets like password material.
    """
    ns = auth.get("_oauth_cache_id")
    if ns is not None and str(ns).strip():
        return f"rt|{ns}"
    raise ValueError(
        "oauth2 refresh requires _oauth_cache_id for in-memory token caching; "
        "use auth_with_oauth_cache_namespace(auth, unique_id) or set _oauth_cache_id on auth.",
    )


def _normalize_auth_method(raw: str | None) -> str:
    v = (raw or "client_secret_post").strip().lower()
    if v in ("basic", "client_secret_basic", "http_basic"):
        return "client_secret_basic"
    return "client_secret_post"


async def post_oauth2_token_endpoint(
    token_url: str,
    form: dict[str, str],
    *,
    client_id: str,
    client_secret: str,
    token_endpoint_auth_method: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST ``application/x-www-form-urlencoded`` to the token endpoint.

    ``client_secret_basic`` (RFC 6749 §2.3.1): ``Authorization: Basic``; client id/secret are not
    duplicated in the form body.
    ``client_secret_post``: ``client_id`` / ``client_secret`` are added to ``form`` if not present.
    """
    method = _normalize_auth_method(token_endpoint_auth_method)
    headers: dict[str, str] = {"Content-Type": "application/x-www-form-urlencoded"}
    data = dict(form)

    if method == "client_secret_basic":
        raw = f"{client_id}:{client_secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        data.pop("client_id", None)
        data.pop("client_secret", None)
    else:
        if client_id and "client_id" not in data:
            data["client_id"] = client_id
        if client_secret and "client_secret" not in data:
            data["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(token_url, data=data, headers=headers)
        r.raise_for_status()
        return r.json()


def _parse_expires(body: dict[str, Any], now: float) -> tuple[str, float]:
    token = body.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("OAuth2 token response missing access_token")
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        ttl = float(expires_in)
    else:
        ttl = 3600.0
    return token.strip(), now + ttl - 30.0


async def fetch_oauth2_client_credentials_token(
    auth: dict[str, str | int | float | bool | None],
    *,
    timeout: float = 30.0,
) -> str:
    """Obtain an access token (RFC 6749 ``client_credentials``). Cached until shortly before expiry."""
    token_url = str(auth.get("token_url") or "").strip()
    client_id = str(auth.get("client_id") or "").strip()
    client_secret = str(auth.get("client_secret") or "").strip()
    if not token_url or not client_id:
        raise ValueError("oauth2_client_credentials requires token_url and client_id")

    key = _client_credentials_cache_key(auth)
    now = time.monotonic()
    cached = _TOKEN_CACHE.get(key)
    if cached is not None and now < cached.expires_at:
        return cached.access_token

    scope = str(auth.get("scope") or "").strip()
    auth_method = str(auth.get("token_endpoint_auth_method") or "client_secret_post")
    data: dict[str, str] = {"grant_type": "client_credentials"}
    if scope:
        data["scope"] = scope

    body = await post_oauth2_token_endpoint(
        token_url,
        data,
        client_id=client_id,
        client_secret=client_secret,
        token_endpoint_auth_method=auth_method,
        timeout=timeout,
    )
    access, exp = _parse_expires(body, now)
    _TOKEN_CACHE[key] = _TokenState(access_token=access, expires_at=exp, refresh_token=None)
    return access


async def fetch_oauth2_refresh_access_token(
    auth: dict[str, str | int | float | bool | None],
    *,
    timeout: float = 30.0,
) -> str:
    """Obtain an access token via ``grant_type=refresh_token`` (RFC 6749).

    Caches the access token. If the issuer returns a new ``refresh_token``, it replaces the stored
    one for this cache entry (**rotation**) so subsequent refreshes use the latest secret.

    ``auth`` must include ``_oauth_cache_id`` (see :func:`auth_with_oauth_cache_namespace`) so the
    cache is partitioned without deriving keys from ``refresh_token``.
    """
    token_url = str(auth.get("token_url") or "").strip()
    client_id = str(auth.get("client_id") or "").strip()
    client_secret = str(auth.get("client_secret") or "").strip()
    if not token_url or not client_id:
        raise ValueError("oauth2 refresh flow requires token_url and client_id")

    key = _refresh_cache_key(auth)
    now = time.monotonic()
    state = _TOKEN_CACHE.get(key)

    if state is not None and now < state.expires_at:
        return state.access_token

    refresh = str(auth.get("refresh_token") or "").strip()
    if state is not None and state.refresh_token:
        refresh = state.refresh_token
    if not refresh:
        raise ValueError("oauth2 refresh flow requires refresh_token (in config or from prior rotation)")

    scope = str(auth.get("scope") or "").strip()
    auth_method = str(auth.get("token_endpoint_auth_method") or "client_secret_post")
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }
    if scope:
        data["scope"] = scope

    body = await post_oauth2_token_endpoint(
        token_url,
        data,
        client_id=client_id,
        client_secret=client_secret,
        token_endpoint_auth_method=auth_method,
        timeout=timeout,
    )
    access, exp = _parse_expires(body, now)

    new_rt = body.get("refresh_token")
    if isinstance(new_rt, str) and new_rt.strip():
        stored_refresh = new_rt.strip()
    else:
        stored_refresh = refresh

    _TOKEN_CACHE[key] = _TokenState(
        access_token=access,
        expires_at=exp,
        refresh_token=stored_refresh,
    )
    return access


async def exchange_oauth2_authorization_code(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    scope: str | None = None,
    token_endpoint_auth_method: str = "client_secret_post",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Exchange an authorization code for tokens (RFC 6749 §4.1.3).

    Returns the parsed JSON body (typically ``access_token``, ``expires_in``, ``refresh_token``, …).
    Does not use the in-memory cache — callers should persist ``refresh_token`` and configure
    ``type: oauth2_refresh`` (or ``oauth2`` with ``grant_type: refresh_token``) for ongoing API access.
    """
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code.strip(),
        "redirect_uri": redirect_uri.strip(),
    }
    if scope and scope.strip():
        data["scope"] = scope.strip()

    return await post_oauth2_token_endpoint(
        token_url.strip(),
        data,
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
        token_endpoint_auth_method=token_endpoint_auth_method,
        timeout=timeout,
    )


def clear_oauth2_token_cache_for_tests() -> None:
    """Test helper — clear in-memory OAuth token cache."""
    _TOKEN_CACHE.clear()
