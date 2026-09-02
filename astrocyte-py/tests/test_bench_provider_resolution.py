"""Tests for the bench harness's env-driven LLM provider resolution.

``_build_llm_provider_from_env`` is the configurability contract: the bench
must be able to run against ANY installed provider adapter without editing
harness code. These tests pin that contract — in particular that no provider
name is special-cased in the resolution logic.

Env surface mirrors the ``astrocyte.yaml`` keys 1:1:
    ASTROCYTE_LLM_PROVIDER / _CONFIG
    ASTROCYTE_EMBEDDING_PROVIDER / _CONFIG
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The harness lives in scripts/, outside the installed package.
_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from mem0_harness.astrocyte_client import (  # noqa: E402
    _build_llm_provider_from_env,
)

_ENV_KEYS = (
    "ASTROCYTE_LLM_PROVIDER",
    "ASTROCYTE_LLM_PROVIDER_CONFIG",
    "ASTROCYTE_EMBEDDING_PROVIDER",
    "ASTROCYTE_EMBEDDING_PROVIDER_CONFIG",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Every test starts from an unset provider environment."""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    # OpenAIProvider validates key presence at construction.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")


class TestDefaultBehaviour:
    def test_defaults_to_openai_preserving_prior_bench_behaviour(self):
        provider = _build_llm_provider_from_env()
        assert type(provider).__name__ == "OpenAIProvider"

    def test_no_embedding_provider_means_no_composite_wrapper(self):
        provider = _build_llm_provider_from_env()
        assert type(provider).__name__ != "CompositeLLMProvider"


class TestEntryPointResolution:
    def test_resolves_registered_entry_point_by_name(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER", "mock")
        assert type(_build_llm_provider_from_env()).__name__ == "MockLLMProvider"

    def test_hyphenated_alias_normalises_to_entry_point_name(self, monkeypatch):
        """`claude-cli` (CLI-style) must resolve to the `claude_cli` entry point."""
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER", "claude-cli")
        monkeypatch.setenv("CLAUDE_CLI_BIN", "/usr/bin/true")
        assert type(_build_llm_provider_from_env()).__name__ == "ClaudeCliProvider"

    def test_accepts_direct_module_colon_class_path(self, monkeypatch):
        monkeypatch.setenv(
            "ASTROCYTE_LLM_PROVIDER", "astrocyte.testing.in_memory:MockLLMProvider",
        )
        assert type(_build_llm_provider_from_env()).__name__ == "MockLLMProvider"

    def test_unknown_provider_name_raises_lookup_error(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER", "no_such_provider")
        with pytest.raises(LookupError, match="no_such_provider"):
            _build_llm_provider_from_env()


class TestProviderConfig:
    def test_json_config_is_passed_as_constructor_kwargs(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER", "claude_cli")
        monkeypatch.setenv(
            "ASTROCYTE_LLM_PROVIDER_CONFIG",
            '{"model": "sonnet", "binary": "/usr/bin/true"}',
        )
        provider = _build_llm_provider_from_env()
        assert provider._model == "sonnet"

    def test_malformed_json_config_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER", "mock")
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER_CONFIG", "{not json")
        with pytest.raises(RuntimeError, match="not valid JSON"):
            _build_llm_provider_from_env()

    def test_non_object_json_config_is_rejected(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER", "mock")
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER_CONFIG", '["not", "an", "object"]')
        with pytest.raises(RuntimeError, match="must be a JSON object"):
            _build_llm_provider_from_env()

    def test_empty_config_string_is_treated_as_no_kwargs(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER", "mock")
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER_CONFIG", "")
        assert type(_build_llm_provider_from_env()).__name__ == "MockLLMProvider"


class TestCompositeComposition:
    def test_split_providers_are_composed(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER", "claude_cli")
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER_CONFIG", '{"binary": "/usr/bin/true"}')
        monkeypatch.setenv("ASTROCYTE_EMBEDDING_PROVIDER", "mock")

        provider = _build_llm_provider_from_env()
        assert type(provider).__name__ == "CompositeLLMProvider"
        assert type(provider._completion).__name__ == "ClaudeCliProvider"
        assert type(provider._embedding).__name__ == "MockLLMProvider"

    def test_embedding_provider_config_reaches_the_embedder(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER", "mock")
        monkeypatch.setenv("ASTROCYTE_EMBEDDING_PROVIDER", "claude_cli")
        monkeypatch.setenv(
            "ASTROCYTE_EMBEDDING_PROVIDER_CONFIG",
            '{"model": "opus", "binary": "/usr/bin/true"}',
        )
        provider = _build_llm_provider_from_env()
        assert provider._embedding._model == "opus"

    def test_claude_native_configuration_end_to_end(self, monkeypatch):
        """The documented zero-OpenAI bench configuration must resolve."""
        # sentence-transformers ships in the `rerank` extra, not `dev`; stub it
        # so resolution is exercised without pulling torch into the test env.
        import types as _types

        st = _types.ModuleType("sentence_transformers")
        st.SentenceTransformer = object
        monkeypatch.setitem(sys.modules, "sentence_transformers", st)

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER", "claude_cli")
        monkeypatch.setenv("ASTROCYTE_LLM_PROVIDER_CONFIG", '{"model": "haiku"}')
        monkeypatch.setenv("ASTROCYTE_EMBEDDING_PROVIDER", "local_embeddings")
        monkeypatch.setenv("CLAUDE_CLI_BIN", "/usr/bin/true")

        provider = _build_llm_provider_from_env()
        assert type(provider).__name__ == "CompositeLLMProvider"
        assert provider._completion._model == "haiku"
        assert type(provider._embedding).__name__ == "LocalEmbeddingsProvider"


class TestRegisteredEntryPoints:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("openai", "OpenAIProvider"),
            ("mock", "MockLLMProvider"),
            ("claude_cli", "ClaudeCliProvider"),
            ("local_embeddings", "LocalEmbeddingsProvider"),
        ],
    )
    def test_provider_is_discoverable_via_the_shared_registry(self, name, expected):
        """Same registry the library and gateway use — so these names are
        equally valid as ``llm_provider:`` in astrocyte.yaml."""
        from astrocyte._discovery import resolve_provider

        assert resolve_provider(name, "llm_providers").__name__ == expected
