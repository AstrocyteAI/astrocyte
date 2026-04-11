"""M4 — webhook payload handling → retain (TDD)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from astrocyte.config import SourceConfig
from astrocyte.ingest.hmac_auth import compute_hmac_sha256_hex
from astrocyte.ingest.webhook import WebhookIngestResult, handle_webhook_ingest
from astrocyte.types import RetainResult


def _webhook_source() -> SourceConfig:
    return SourceConfig(
        type="webhook",
        target_bank="ingest-bank",
        extraction_profile="builtin_text",
        auth={"type": "hmac", "secret": "whsec_test", "header": "X-Webhook-Signature"},
    )


@pytest.mark.asyncio
class TestHandleWebhookIngest:
    async def test_valid_hmac_calls_retain(self):
        cfg = _webhook_source()
        body = b'{"content":"hello from webhook"}'
        sig = compute_hmac_sha256_hex("whsec_test", body)
        headers = {"x-webhook-signature": sig}

        retain = AsyncMock(return_value=RetainResult(stored=True, memory_id="m1"))

        result = await handle_webhook_ingest(
            source_id="tavus",
            source_config=cfg,
            raw_body=body,
            headers=headers,
            retain=retain,
        )

        assert isinstance(result, WebhookIngestResult)
        assert result.ok is True
        assert result.http_status == 200
        assert result.retain_result is not None
        assert result.retain_result.stored is True
        retain.assert_awaited_once()
        assert retain.await_args.args[0] == "hello from webhook"
        assert retain.await_args.args[1] == "ingest-bank"
        assert retain.await_args.kwargs.get("extraction_profile") == "builtin_text"

    async def test_invalid_hmac_returns_401(self):
        cfg = _webhook_source()
        body = b'{"content":"x"}'
        headers = {"x-webhook-signature": "deadbeef"}

        retain = AsyncMock()

        result = await handle_webhook_ingest(
            source_id="tavus",
            source_config=cfg,
            raw_body=body,
            headers=headers,
            retain=retain,
        )

        assert result.ok is False
        assert result.http_status == 401
        retain.assert_not_called()

    async def test_missing_content_returns_400(self):
        cfg = _webhook_source()
        body = b"{}"
        sig = compute_hmac_sha256_hex("whsec_test", body)
        headers = {"x-webhook-signature": sig}
        retain = AsyncMock()

        result = await handle_webhook_ingest(
            source_id="tavus",
            source_config=cfg,
            raw_body=body,
            headers=headers,
            retain=retain,
        )

        assert result.ok is False
        assert result.http_status == 400
        retain.assert_not_called()
