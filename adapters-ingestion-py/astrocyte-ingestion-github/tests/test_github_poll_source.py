"""Unit tests for GithubPollIngestSource."""

from __future__ import annotations

import httpx
import pytest
from astrocyte.config import SourceConfig

from astrocyte_ingestion_github import GithubPollIngestSource


@pytest.mark.asyncio
async def test_poll_once_skips_pull_requests_and_recalls_retain() -> None:
    retained: list[tuple[str, str, dict[str, object]]] = []

    async def retain(text: str, bank_id: str, **kwargs: object) -> object:
        from astrocyte.types import RetainResult

        meta = kwargs.get("metadata")
        retained.append((text, bank_id, meta if isinstance(meta, dict) else {}))
        return RetainResult(stored=True, memory_id="m1")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "/repos/o/r/issues" in str(request.url)
        payload = [
            {
                "id": 1001,
                "updated_at": "2024-01-02T00:00:00Z",
                "title": "Hello",
                "body": "World",
                "number": 10,
                "html_url": "https://github.com/o/r/issues/10",
                "user": {"login": "bob"},
            },
                {
                    "id": 1002,
                    "updated_at": "2024-01-01T00:00:00Z",
                    "title": "PR",
                    "pull_request": {
                        "url": "https://api.github.com/repos/o/r/pulls/1",
                        "html_url": "https://github.com/o/r/pull/1",
                    },
                },
        ]
        return httpx.Response(200, json=payload)

    cfg = SourceConfig(
        type="poll",
        driver="github",
        path="o/r",
        interval_seconds=60,
        target_bank="bank-a",
        auth={"token": "ghp_test_token"},
    )
    src = GithubPollIngestSource("gh", cfg, retain=retain)
    src._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer ghp_test_token",
        },
    )
    try:
        await src._poll_once()
    finally:
        await src._client.aclose()
        src._client = None

    assert len(retained) == 1
    assert retained[0][1] == "bank-a"
    assert "[GitHub #10]" in retained[0][0]
    assert "Hello" in retained[0][0]
    assert retained[0][2].get("github", {}).get("number") == 10
