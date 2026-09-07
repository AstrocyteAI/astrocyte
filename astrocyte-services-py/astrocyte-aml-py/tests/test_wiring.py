"""Brain wiring for the container entrypoint.

Regression cover for a submission-blocking defect: ``Astrocyte.from_config()``
returns a brain with no pipeline, so the adapter's ``ASTROCYTE_CONFIG_PATH``
fallback used to start cleanly and then fail on AML's first ``/add``. These
tests pin both halves — that wiring produces a usable brain, and that a
misconfigured service reports itself unready instead of green.
"""

from __future__ import annotations

import pytest
from astrocyte.errors import ConfigError
from fastapi.testclient import TestClient

from astrocyte_aml.app import create_app
from astrocyte_aml.wiring import build_brain

CONFIG = """
provider_tier: storage
vector_store: in_memory
llm_provider: mock
access_control:
  enabled: false
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "astrocyte.yaml"
    path.write_text(CONFIG)
    return str(path)


class TestBuildBrain:
    def test_produces_a_brain_with_a_pipeline(self, config_file):
        # The bug: from_config() alone leaves _pipeline None.
        brain = build_brain(config_file)
        assert brain._pipeline is not None

    @pytest.mark.asyncio
    async def test_the_brain_can_actually_retain(self, config_file):
        # The symptom that would have hit AML's first /add.
        brain = build_brain(config_file)
        result = await brain.retain("Alice owns payments.", bank_id="default")
        assert result.stored is True

    def test_missing_config_path_is_a_clear_error(self, monkeypatch):
        monkeypatch.delenv("ASTROCYTE_CONFIG_PATH", raising=False)
        with pytest.raises(ConfigError, match="ASTROCYTE_CONFIG_PATH"):
            build_brain(None)

    def test_reads_the_env_var_when_no_argument_given(self, config_file, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", config_file)
        assert build_brain(None)._pipeline is not None

    def test_missing_vector_store_is_rejected(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("provider_tier: storage\nllm_provider: mock\n")
        with pytest.raises(ConfigError, match="vector_store"):
            build_brain(str(path))

    def test_missing_llm_provider_is_rejected(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("provider_tier: storage\nvector_store: in_memory\n")
        with pytest.raises(ConfigError, match="llm_provider"):
            build_brain(str(path))

    def test_unknown_provider_name_surfaces_as_an_error(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("provider_tier: storage\nvector_store: nope-not-real\nllm_provider: mock\n")
        with pytest.raises(Exception):
            build_brain(str(path))


class TestHealthReportsReadiness:
    def test_healthy_when_configured(self, config_file, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", config_file)
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200

    def test_unready_when_misconfigured(self, monkeypatch):
        # A static "ok" here would hand the evaluator a green but broken service.
        monkeypatch.delenv("ASTROCYTE_CONFIG_PATH", raising=False)
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            resp = client.get("/health")
        assert resp.status_code == 503
        assert "not ready" in resp.json()["error"]

    def test_injected_brain_bypasses_config(self):
        # create_app(brain=...) is the test/embedding path and must stay green
        # without any ASTROCYTE_CONFIG_PATH.
        class _Brain:
            pass

        with TestClient(create_app(brain=_Brain())) as client:
            assert client.get("/health").status_code == 200


class TestSplitEmbeddingBackend:
    """`embedding_provider` must be honoured, not silently ignored."""

    def test_composes_when_a_separate_embedder_is_configured(self, tmp_path):
        from astrocyte.providers.composite import CompositeLLMProvider

        from astrocyte_aml.wiring import build_pipeline
        from astrocyte.config import load_config

        path = tmp_path / "c.yaml"
        path.write_text(
            "provider_tier: storage\nvector_store: in_memory\n"
            # Two DIFFERENT names: the shared resolver deliberately does not
            # compose a provider with itself, since that only adds indirection.
            # claude_cli is the real motivating case — it cannot embed at all.
            "llm_provider: claude_cli\nembedding_provider: mock\n"
        )
        pipeline = build_pipeline(load_config(str(path)))
        # PipelineOrchestrator wraps providers in _TrackingLLMProvider for
        # token accounting, so unwrap before checking what we actually built.
        inner = getattr(pipeline.llm_provider, "_inner", pipeline.llm_provider)
        assert isinstance(inner, CompositeLLMProvider)

    def test_single_provider_is_left_alone(self, config_file):
        from astrocyte.providers.composite import CompositeLLMProvider

        from astrocyte_aml.wiring import build_pipeline
        from astrocyte.config import load_config

        pipeline = build_pipeline(load_config(config_file))
        inner = getattr(pipeline.llm_provider, "_inner", pipeline.llm_provider)
        assert not isinstance(inner, CompositeLLMProvider)
