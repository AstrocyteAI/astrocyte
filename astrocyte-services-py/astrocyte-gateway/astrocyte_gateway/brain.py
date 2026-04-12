"""Construct `Astrocyte` and Tier 1 pipeline from config (entry points or `module:Class` paths)."""

from __future__ import annotations

import os
from pathlib import Path

from astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig, access_grants_for_astrocyte, load_config

from astrocyte_gateway.wiring import build_tier1_pipeline


def _apply_dev_defaults_when_no_config_file(config: AstrocyteConfig) -> None:
    """Match previous reference behavior: permissive defaults only if no YAML file is loaded."""
    path = os.environ.get("ASTROCYTE_CONFIG_PATH")
    if path and Path(path).is_file():
        return
    config.barriers.pii.mode = "disabled"
    config.escalation.degraded_mode = "error"
    config.access_control.enabled = False


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
    return config


def build_astrocyte() -> Astrocyte:
    """Load config, wire Tier 1 `PipelineOrchestrator` from provider names + entry points."""
    config = _load_astrocyte_config()
    config.provider_tier = "storage"
    _apply_dev_defaults_when_no_config_file(config)

    brain = Astrocyte(config)
    pipeline = build_tier1_pipeline(config)
    brain.set_pipeline(pipeline)
    if config.access_control.enabled:
        brain.set_access_grants(access_grants_for_astrocyte(config))
    return brain


def build_reference_astrocyte() -> Astrocyte:
    """Backward-compatible name for `build_astrocyte()`."""
    return build_astrocyte()
