"""Async client for the Tavus REST API (`x-api-key` auth).

API reference: https://docs.tavus.io/api-reference/overview
"""

from __future__ import annotations

from typing import Any

import httpx

from astrocyte_integration_tavus.exceptions import TavusAPIError

DEFAULT_BASE_URL = "https://tavusapi.com/v2"


class TavusClient:
    """Minimal async HTTP client for Tavus **v2** endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ValueError("api_key is required")
        self._api_key = key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        self._client.headers["x-api-key"] = self._api_key

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> TavusClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        # Paths must be relative (no leading ``/``) so ``base_url`` ``.../v2/`` is preserved.
        rel = path.lstrip("/")
        response = await self._client.request(method, rel, params=params)
        if response.status_code >= 400:
            body = response.text[:4096] if response.text else None
            raise TavusAPIError(
                f"Tavus API {method} {path} failed: {response.status_code}",
                status_code=response.status_code,
                body=body,
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def list_conversations(
        self,
        *,
        limit: int | None = None,
        page: int | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """``GET /conversations`` — paginated list (`data`, `total_count`)."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if page is not None:
            params["page"] = page
        if status is not None:
            params["status"] = status
        out = await self._request("GET", "conversations", params=params or None)
        return out if isinstance(out, dict) else {}

    async def get_conversation(
        self,
        conversation_id: str,
        *,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """``GET /conversations/{id}`` — optional ``verbose=true`` for transcript-rich payload."""
        cid = (conversation_id or "").strip()
        if not cid:
            raise ValueError("conversation_id is required")
        params: dict[str, Any] = {}
        if verbose:
            params["verbose"] = "true"
        out = await self._request(
            "GET",
            f"conversations/{cid}",
            params=params or None,
        )
        return out if isinstance(out, dict) else {}
