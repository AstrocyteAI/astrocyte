from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest
from astrocyte_gateway.app import _warm_reference_stack_provider, _warm_reference_stack_providers
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
    # Default tenant extension yields a single tenant ("public") → exactly one worker.
    assert len(worker.tenants) == 1
    only_tenant = worker.tenants[0]
    assert only_tenant.schema == "public"
    assert isinstance(only_tenant.queue, PgQueuerMemoryTaskQueue)
    assert only_tenant.worker_task is None
    await worker.stop()


@pytest.mark.anyio
async def test_gateway_startup_warms_graph_provider() -> None:
    class WarmableGraphStore:
        def __init__(self) -> None:
            self.warmed = False
            self.schema_bootstrapped = False

        async def _ensure_schema(self):
            self.schema_bootstrapped = True

        async def health(self):
            self.warmed = True

    graph_store = WarmableGraphStore()
    brain = SimpleNamespace(
        _pipeline=SimpleNamespace(graph_store=graph_store),
        _wiki_store=None,
    )

    await _warm_reference_stack_providers(brain)

    assert graph_store.warmed is True
    assert graph_store.schema_bootstrapped is True


@pytest.mark.anyio
async def test_gateway_startup_rejects_unhealthy_reference_provider() -> None:
    class UnhealthyProvider:
        async def health(self):
            return SimpleNamespace(healthy=False, message="schema failed")

    with pytest.raises(ConfigError, match="schema failed"):
        await _warm_reference_stack_provider(UnhealthyProvider())


@pytest.mark.anyio
async def test_gateway_startup_skips_schema_helper_that_requires_arguments() -> None:
    class PoolBackedProvider:
        def __init__(self) -> None:
            self.warmed = False

        async def _ensure_schema(self, pool):
            raise AssertionError("pool-backed schema helper should be warmed through health")

        async def health(self):
            self.warmed = True

    provider = PoolBackedProvider()

    await _warm_reference_stack_provider(provider)

    assert provider.warmed is True
