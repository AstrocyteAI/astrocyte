from __future__ import annotations

import pytest
from astrocyte_gateway.brain import build_astrocyte
from astrocyte_gateway.wiring import build_tier1_pipeline, resolve_wiki_store

from astrocyte.config import AstrocyteConfig
from astrocyte.errors import ConfigError


def test_gateway_wires_entity_resolver_when_graph_store_is_configured() -> None:
    config = AstrocyteConfig(
        provider_tier="storage",
        vector_store="in_memory",
        graph_store="in_memory",
        llm_provider="mock",
    )
    config.entity_resolution.enabled = True

    pipeline = build_tier1_pipeline(config)

    assert pipeline.entity_resolver is not None


def test_gateway_rejects_entity_resolution_without_graph_store() -> None:
    config = AstrocyteConfig(
        provider_tier="storage",
        vector_store="in_memory",
        llm_provider="mock",
    )
    config.entity_resolution.enabled = True

    with pytest.raises(ConfigError, match="requires a graph_store"):
        build_tier1_pipeline(config)


def test_gateway_resolves_wiki_store_provider() -> None:
    config = AstrocyteConfig(provider_tier="storage", wiki_store="in_memory")

    wiki_store = resolve_wiki_store(config)

    assert wiki_store is not None


def test_build_astrocyte_attaches_compile_queue_when_auto_start_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config_path = tmp_path / "astrocyte.yaml"
    config_path.write_text(
        "\n".join(
            [
                "provider_tier: storage",
                "vector_store: in_memory",
                "graph_store: in_memory",
                "wiki_store: in_memory",
                "llm_provider: mock",
                "wiki_compile:",
                "  enabled: true",
                "  auto_start: true",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(config_path))

    brain = build_astrocyte()

    assert brain.config.wiki_compile.auto_start is True
    assert getattr(brain, "_compile_queue") is not None
