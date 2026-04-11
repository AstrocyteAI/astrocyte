"""M4 — webhook handler → Astrocyte.retain (integration)."""

from __future__ import annotations

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig, SourceConfig
from astrocyte.ingest.hmac_auth import compute_hmac_sha256_hex
from astrocyte.ingest.source import IngestSource, WebhookIngestSource
from astrocyte.ingest.webhook import handle_webhook_ingest
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider


def _tier1_brain() -> tuple[Astrocyte, InMemoryVectorStore]:
    config = AstrocyteConfig()
    config.provider_tier = "storage"
    config.barriers.pii.mode = "disabled"
    config.escalation.degraded_mode = "error"
    brain = Astrocyte(config)
    vs = InMemoryVectorStore()
    brain.set_pipeline(PipelineOrchestrator(vector_store=vs, llm_provider=MockLLMProvider()))
    return brain, vs


class TestIngestSourceProtocol:
    def test_webhook_source_is_instance(self):
        cfg = SourceConfig(type="webhook", target_bank="b1")
        src = WebhookIngestSource("sid", cfg)
        assert isinstance(src, IngestSource)


class TestWebhookAstrocyteIntegration:
    async def test_handle_webhook_stores_via_retain(self):
        brain, vs = _tier1_brain()
        cfg = SourceConfig(
            type="webhook",
            target_bank="bank-int",
            auth={"type": "hmac", "secret": "sec", "header": "X-Sig"},
        )
        body = b'{"content":"integrated memory"}'
        sig = compute_hmac_sha256_hex("sec", body)

        result = await handle_webhook_ingest(
            source_id="src1",
            source_config=cfg,
            raw_body=body,
            headers={"X-Sig": sig},
            retain=brain.retain,
        )

        assert result.ok is True
        assert result.retain_result is not None
        assert result.retain_result.stored is True
        assert len(vs._vectors) == 1
        stored = next(iter(vs._vectors.values()))
        assert "integrated memory" in stored.text
