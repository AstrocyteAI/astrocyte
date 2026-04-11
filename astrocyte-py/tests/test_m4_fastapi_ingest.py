"""M4 — ASGI ingest route (optional ``astrocyte[gateway]``; Starlette app)."""

from __future__ import annotations

import pytest

pytest.importorskip("starlette")

from starlette.testclient import TestClient

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig, SourceConfig
from astrocyte.ingest.fastapi_app import create_ingest_webhook_app
from astrocyte.ingest.hmac_auth import compute_hmac_sha256_hex
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider


def _brain_with_pipeline() -> Astrocyte:
    cfg = AstrocyteConfig()
    cfg.provider_tier = "storage"
    cfg.barriers.pii.mode = "disabled"
    cfg.escalation.degraded_mode = "error"
    brain = Astrocyte(cfg)
    brain.set_pipeline(
        PipelineOrchestrator(vector_store=InMemoryVectorStore(), llm_provider=MockLLMProvider()),
    )
    return brain


def test_webhook_route_unknown_source_404() -> None:
    brain = _brain_with_pipeline()
    app = create_ingest_webhook_app(brain, {})
    client = TestClient(app)
    r = client.post("/v1/ingest/webhook/missing", content=b"{}")
    assert r.status_code == 404


def test_webhook_route_happy_path() -> None:
    brain = _brain_with_pipeline()
    secret = "whsec"
    sources = {
        "src1": SourceConfig(
            type="webhook",
            target_bank="bank-w",
            auth={"type": "hmac", "secret": secret, "header": "X-Sig"},
        ),
    }
    app = create_ingest_webhook_app(brain, sources)
    client = TestClient(app)
    body = b'{"content":"via fastapi"}'
    sig = compute_hmac_sha256_hex(secret, body)
    r = client.post("/v1/ingest/webhook/src1", content=body, headers={"X-Sig": sig})
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data.get("stored") is True
