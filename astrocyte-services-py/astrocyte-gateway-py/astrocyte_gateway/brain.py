"""Construct `Astrocyte` and Tier 1 pipeline from config (entry points or `module:Class` paths)."""

from __future__ import annotations

import os
from pathlib import Path

from astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig, access_grants_for_astrocyte, load_config
from astrocyte_gateway.wiring import build_tier1_pipeline, resolve_wiki_store


def _apply_dev_defaults_when_no_config_file(config: AstrocyteConfig) -> None:
    """Match previous reference behavior: permissive defaults only if no YAML file is loaded."""
    path = os.environ.get("ASTROCYTE_CONFIG_PATH")
    if path and Path(path).is_file():
        return
    config.barriers.pii.mode = "disabled"
    config.escalation.degraded_mode = "error"
    config.access_control.enabled = False


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def _load_astrocyte_config() -> AstrocyteConfig:
    path = os.environ.get("ASTROCYTE_CONFIG_PATH")
    if path and Path(path).is_file():
        return load_config(path)
    config = AstrocyteConfig()
    if v := os.environ.get("ASTROCYTE_VECTOR_STORE"):
        config.vector_store = v
    if v := os.environ.get("ASTROCYTE_LLM_PROVIDER"):
        config.llm_provider = v
    if v := os.environ.get("ASTROCYTE_GRAPH_STORE"):
        config.graph_store = v
    if v := os.environ.get("ASTROCYTE_DOCUMENT_STORE"):
        config.document_store = v
    if v := os.environ.get("ASTROCYTE_WIKI_STORE"):
        config.wiki_store = v
    if (v := _env_bool("ASTROCYTE_WIKI_COMPILE_ENABLED")) is not None:
        config.wiki_compile.enabled = v
    if (v := _env_bool("ASTROCYTE_WIKI_COMPILE_AUTO_START")) is not None:
        config.wiki_compile.auto_start = v
    if (v := _env_bool("ASTROCYTE_ENTITY_RESOLUTION_ENABLED")) is not None:
        config.entity_resolution.enabled = v
    if (v := _env_bool("ASTROCYTE_ASYNC_TASKS_ENABLED")) is not None:
        config.async_tasks.enabled = v
    if v := os.environ.get("ASTROCYTE_ASYNC_TASKS_BACKEND"):
        config.async_tasks.backend = v
    if v := os.environ.get("ASTROCYTE_TASKS_DSN"):
        config.async_tasks.dsn = v
    if (v := _env_bool("ASTROCYTE_ASYNC_TASKS_INSTALL_ON_START")) is not None:
        config.async_tasks.install_on_start = v
    if (v := _env_bool("ASTROCYTE_ASYNC_TASKS_AUTO_START_WORKER")) is not None:
        config.async_tasks.auto_start_worker = v
    return config


def build_astrocyte() -> Astrocyte:
    """Load config, wire Tier 1 `PipelineOrchestrator` from provider names + entry points."""
    config = _load_astrocyte_config()
    config.provider_tier = "storage"
    _apply_dev_defaults_when_no_config_file(config)

    brain = Astrocyte(config)
    wiki_store = resolve_wiki_store(config)
    pipeline = build_tier1_pipeline(config, wiki_store=wiki_store)
    brain.set_pipeline(pipeline)
    if wiki_store is not None:
        brain.set_wiki_store(wiki_store)
        if config.wiki_compile.auto_start:
            from astrocyte.pipeline.compile import CompileEngine
            from astrocyte.pipeline.compile_trigger import CompileQueue, CompileTriggerConfig

            compile_engine = CompileEngine(
                vector_store=pipeline.vector_store,
                llm_provider=pipeline.llm_provider,
                wiki_store=wiki_store,
            )
            brain.set_compile_queue(
                CompileQueue(
                    compile_engine,
                    CompileTriggerConfig(
                        size_threshold=config.wiki_compile.size_threshold,
                        staleness_days=config.wiki_compile.staleness_days,
                        staleness_min_memories=config.wiki_compile.staleness_min_memories,
                    ),
                    max_queue_size=config.wiki_compile.max_queue_size,
                )
            )
    if config.access_control.enabled:
        brain.set_access_grants(access_grants_for_astrocyte(config))
    return brain


def build_reference_astrocyte() -> Astrocyte:
    """Backward-compatible name for `build_astrocyte()`."""
    return build_astrocyte()
