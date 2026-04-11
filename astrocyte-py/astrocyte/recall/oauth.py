"""OAuth2 client-credentials token fetch for proxy recall (M4.1)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_TOKEN_CACHE: dict[str, _TokenState] = {}


@dataclass
class _TokenState:
    access_token: str
    expires_at: float  # monotonic


def _cache_key(auth: dict[str, str | int | float | bool | None]) -> str:
    tid = str(auth.get("token_url") or "")
    cid = str(auth.get("client_id") or "")
    sc = str(auth.get("scope") or "")
    return f"{tid}|{cid}|{sc}"


async def fetch_oauth2_client_credentials_token(
    auth: dict[str, str | int | float | bool | None],
    *,
    timeout: float = 30.0,
) -> str:
    """Obtain an access token (RFC 6749 client_credentials). Tokens are cached until shortly before expiry."""
    token_url = str(auth.get("token_url") or "").strip()
    client_id = str(auth.get("client_id") or "").strip()
    client_secret = str(auth.get("client_secret") or "").strip()
    if not token_url or not client_id:
        raise ValueError("oauth2_client_credentials requires token_url and client_id")

    key = _cache_key(auth)
    now = time.monotonic()
    cached = _TOKEN_CACHE.get(key)
    if cached is not None and now < cached.expires_at:
        return cached.access_token

    scope = str(auth.get("scope") or "").strip()
    data: dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        data["scope"] = scope

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        body = r.json()

    token = body.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("OAuth2 token response missing access_token")

    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        ttl = float(expires_in)
    else:
        ttl = 3600.0
    # Refresh slightly before expiry
    _TOKEN_CACHE[key] = _TokenState(access_token=token.strip(), expires_at=now + ttl - 30.0)
    return token.strip()


def clear_oauth2_token_cache_for_tests() -> None:
    """Test helper — clear in-memory OAuth token cache."""
    _TOKEN_CACHE.clear()
