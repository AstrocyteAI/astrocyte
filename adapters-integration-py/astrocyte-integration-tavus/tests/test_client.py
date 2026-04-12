"""Tests for :class:`TavusClient` (mocked HTTP)."""

from __future__ import annotations

import httpx
import pytest

from astrocyte_integration_tavus import TavusAPIError, TavusClient


@pytest.mark.asyncio
async def test_list_conversations_sends_api_key_and_query() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-api-key")
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={"data": [{"conversation_id": "c1"}], "total_count": 1},
        )

    async with TavusClient(
        "secret-key",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://tavusapi.com/v2/",
        ),
    ) as tc:
        out = await tc.list_conversations(limit=5, page=2, status="ended")

    assert out["total_count"] == 1
    assert "limit=5" in str(captured["url"])
    assert "page=2" in str(captured["url"])
    assert "status=ended" in str(captured["url"])


@pytest.mark.asyncio
async def test_get_conversation_verbose_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/conversations/cx" in str(request.url)
        assert "verbose=true" in str(request.url)
        return httpx.Response(200, json={"conversation_id": "cx", "status": "ended"})

    async with TavusClient(
        "k",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://tavusapi.com/v2/",
        ),
    ) as tc:
        out = await tc.get_conversation("cx", verbose=True)

    assert out["conversation_id"] == "cx"


@pytest.mark.asyncio
async def test_api_error_includes_status() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid access token"})

    async with TavusClient(
        "bad",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://tavusapi.com/v2/",
        ),
    ) as tc:
        with pytest.raises(TavusAPIError) as ei:
            await tc.list_conversations()

    assert ei.value.status_code == 401
    assert ei.value.body is not None


def test_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        TavusClient("  ")
