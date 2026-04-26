from __future__ import annotations

import importlib.util

import pytest
from astrocyte_gateway.brain import build_astrocyte
from astrocyte_gateway.tasks import start_gateway_task_worker
from astrocyte_gateway.wiring import build_tier1_pipeline, resolve_wiki_store

from astrocyte.config import AstrocyteConfig
from astrocyte.errors import ConfigError
from astrocyte.pipeline.pgqueuer_tasks import PgQueuerMemoryTaskQueue


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


def test_build_tier1_pipeline_receives_configured_wiki_store() -> None:
    config = AstrocyteConfig(
        provider_tier="storage",
        vector_store="in_memory",
        wiki_store="in_memory",
        llm_provider="mock",
    )
    wiki_store = resolve_wiki_store(config)

    pipeline = build_tier1_pipeline(config, wiki_store=wiki_store)

    assert pipeline.wiki_store is wiki_store


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


def test_build_astrocyte_parses_async_task_worker_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config_path = tmp_path / "astrocyte.yaml"
    config_path.write_text(
        "\n".join(
            [
                "provider_tier: storage",
                "vector_store: in_memory",
                "wiki_store: in_memory",
                "llm_provider: mock",
                "async_tasks:",
                "  enabled: true",
                "  backend: pgqueuer_in_memory",
                "  install_on_start: true",
                "  auto_start_worker: false",
                "  batch_size: 4",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(config_path))

    brain = build_astrocyte()

    assert brain.config.async_tasks.enabled is True
    assert brain.config.async_tasks.backend == "pgqueuer_in_memory"
    assert brain.config.async_tasks.install_on_start is True
    assert brain.config.async_tasks.batch_size == 4


def test_build_astrocyte_parses_full_reference_stack_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTROCYTE_CONFIG_PATH", raising=False)
    monkeypatch.setenv("ASTROCYTE_VECTOR_STORE", "in_memory")
    monkeypatch.setenv("ASTROCYTE_GRAPH_STORE", "in_memory")
    monkeypatch.setenv("ASTROCYTE_WIKI_STORE", "in_memory")
    monkeypatch.setenv("ASTROCYTE_WIKI_COMPILE_ENABLED", "true")
    monkeypatch.setenv("ASTROCYTE_WIKI_COMPILE_AUTO_START", "true")
    monkeypatch.setenv("ASTROCYTE_ENTITY_RESOLUTION_ENABLED", "true")
    monkeypatch.setenv("ASTROCYTE_ASYNC_TASKS_ENABLED", "true")
    monkeypatch.setenv("ASTROCYTE_ASYNC_TASKS_BACKEND", "pgqueuer_in_memory")
    monkeypatch.setenv("ASTROCYTE_ASYNC_TASKS_INSTALL_ON_START", "true")
    monkeypatch.setenv("ASTROCYTE_ASYNC_TASKS_AUTO_START_WORKER", "false")

    brain = build_astrocyte()

    assert brain.config.wiki_compile.enabled is True
    assert brain.config.wiki_compile.auto_start is True
    assert brain.config.entity_resolution.enabled is True
    assert brain.config.async_tasks.enabled is True
    assert brain.config.async_tasks.backend == "pgqueuer_in_memory"
    assert brain.config.async_tasks.install_on_start is True
    assert brain.config.async_tasks.auto_start_worker is False
    assert getattr(brain, "_wiki_store") is not None
    assert getattr(brain, "_pipeline").wiki_store is getattr(brain, "_wiki_store")


@pytest.mark.anyio
async def test_gateway_starts_pgqueuer_task_worker_with_in_memory_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    if importlib.util.find_spec("pgqueuer") is None:
        pytest.skip("PgQueuer worker extra is not installed")
    config_path = tmp_path / "astrocyte.yaml"
    config_path.write_text(
        "\n".join(
            [
                "provider_tier: storage",
                "vector_store: in_memory",
                "graph_store: in_memory",
                "wiki_store: in_memory",
                "llm_provider: mock",
                "async_tasks:",
                "  enabled: true",
                "  backend: pgqueuer_in_memory",
                "  auto_start_worker: false",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(config_path))
    brain = build_astrocyte()

    worker = await start_gateway_task_worker(brain)

    assert worker is not None
    assert isinstance(worker.queue, PgQueuerMemoryTaskQueue)
    assert worker.worker_task is None
    await worker.stop()
