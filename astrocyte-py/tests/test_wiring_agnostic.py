"""Provider resolution is shared, so one config means one thing everywhere.

The regression these guard against: the gateway and the AML adapter each had
their own resolver, and only one honoured ``embedding_provider``. The same
``astrocyte.yaml`` therefore produced a composite provider in one service and a
bare completion provider in another, with no error — ``retain()`` would fail at
the embedding step against a provider that cannot embed, giving no hint that
the configured embedder was never consulted.
"""

from __future__ import annotations

import pytest

from astrocyte.config import load_config
from astrocyte.errors import ConfigError
from astrocyte.providers.composite import CompositeLLMProvider
from astrocyte.wiring import config_kwargs, instantiate_provider, resolve_llm_provider


def write_config(tmp_path, body: str) -> str:
    path = tmp_path / "astrocyte.yaml"
    path.write_text(body)
    return str(path)


class TestResolveLlmProvider:
    def test_single_provider_is_returned_bare(self, tmp_path):
        cfg = load_config(write_config(tmp_path, "llm_provider: mock\n"))
        assert not isinstance(resolve_llm_provider(cfg), CompositeLLMProvider)

    def test_separate_embedding_provider_composes(self, tmp_path):
        cfg = load_config(write_config(tmp_path, "llm_provider: claude_cli\nembedding_provider: mock\n"))
        assert isinstance(resolve_llm_provider(cfg), CompositeLLMProvider)

    def test_same_name_for_both_roles_is_not_composed(self, tmp_path):
        # Composing a provider with itself only adds indirection.
        cfg = load_config(write_config(tmp_path, "llm_provider: mock\nembedding_provider: mock\n"))
        assert not isinstance(resolve_llm_provider(cfg), CompositeLLMProvider)

    def test_defaults_to_mock_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ASTROCYTE_LLM_PROVIDER", raising=False)
        cfg = load_config(write_config(tmp_path, "vector_store: in_memory\n"))
        assert resolve_llm_provider(cfg) is not None

    def test_env_var_supplies_the_provider_when_config_omits_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER", "mock")
        cfg = load_config(write_config(tmp_path, "vector_store: in_memory\n"))
        assert resolve_llm_provider(cfg) is not None

    def test_unknown_provider_names_itself_in_the_error(self, tmp_path):
        cfg = load_config(write_config(tmp_path, "llm_provider: no-such-provider\n"))
        with pytest.raises(ConfigError, match="no-such-provider"):
            resolve_llm_provider(cfg)

    def test_unknown_embedder_names_itself_in_the_error(self, tmp_path):
        cfg = load_config(write_config(tmp_path, "llm_provider: mock\nembedding_provider: no-such-embedder\n"))
        with pytest.raises(ConfigError, match="no-such-embedder"):
            resolve_llm_provider(cfg)


class TestConfigKwargs:
    def test_none_values_fall_through_to_provider_defaults(self):
        # Passing None would override the provider's own default with nothing.
        assert config_kwargs({"model": "x", "base_url": None}) == {"model": "x"}

    def test_empty_and_missing_config_are_equivalent(self):
        assert config_kwargs(None) == {}
        assert config_kwargs({}) == {}


class TestRegisteredProviders:
    """Names that must stay resolvable — each is a documented config value."""

    @pytest.mark.parametrize("name", ["mock", "openai", "claude_cli", "local_embeddings", "composite", "ollama"])
    def test_provider_name_resolves(self, name):
        from astrocyte._discovery import resolve_provider

        assert resolve_provider(name, "llm_providers") is not None

    def test_ollama_defaults_to_the_local_daemon(self):
        pytest.importorskip("openai")  # construction needs the [openai] extra
        # Discoverability is the point: `llm_provider: ollama` must work without
        # the caller knowing the base_url trick.
        from astrocyte.providers.ollama import DEFAULT_BASE_URL, OllamaProvider

        p = OllamaProvider(model="qwen3:8b")
        assert DEFAULT_BASE_URL.endswith("/v1")
        assert p is not None  # constructs without an API key

    def test_ollama_is_configurable_by_name(self, tmp_path):
        pytest.importorskip("openai")
        cfg = load_config(write_config(tmp_path, "llm_provider: ollama\nllm_provider_config:\n  model: qwen3:8b\n"))
        assert resolve_llm_provider(cfg) is not None


class TestWiringPathsAgree:
    """Every deployment must resolve the same config the same way."""

    CONFIGS = [
        "llm_provider: mock\n",
        "llm_provider: claude_cli\nembedding_provider: mock\n",
        "llm_provider: ollama\nllm_provider_config:\n  model: qwen3:8b\n",
    ]

    @pytest.mark.parametrize("body", CONFIGS)
    def test_gateway_and_core_resolve_the_same_shape(self, tmp_path, body):
        if "ollama" in body:
            pytest.importorskip("openai")
        gateway_wiring = pytest.importorskip("astrocyte_gateway.wiring")
        cfg = load_config(write_config(tmp_path, body))
        assert type(gateway_wiring.resolve_llm_provider(cfg)) is type(resolve_llm_provider(cfg))

    @pytest.mark.parametrize("body", CONFIGS)
    def test_aml_and_core_resolve_the_same_shape(self, tmp_path, body):
        if "ollama" in body:
            pytest.importorskip("openai")
        aml_wiring = pytest.importorskip("astrocyte_aml.wiring")
        cfg = load_config(write_config(tmp_path, body))
        # The adapter builds a whole pipeline; unwrap the orchestrator's token
        # tracker to compare the provider it actually resolved.
        cfg.vector_store = "in_memory"
        pipeline = aml_wiring.build_pipeline(cfg)
        inner = getattr(pipeline.llm_provider, "_inner", pipeline.llm_provider)
        assert type(inner) is type(resolve_llm_provider(cfg))


class TestInstantiateProvider:
    def test_bad_kwargs_produce_a_config_error_naming_the_provider(self):
        with pytest.raises(ConfigError, match="mock"):
            instantiate_provider("mock", "llm_providers", {"not_a_real_kwarg": 1})

    def test_missing_provider_produces_a_config_error_not_a_lookup_error(self):
        with pytest.raises(ConfigError):
            instantiate_provider("nope", "llm_providers")
