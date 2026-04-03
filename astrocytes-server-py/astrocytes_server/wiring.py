"""Wire Tier 1 providers from `AstrocyteConfig` using entry points or `module:Class` paths."""

from __future__ import annotations

import os
from typing import Any, TypeVar

from astrocytes._discovery import resolve_provider
from astrocytes.config import AstrocyteConfig
from astrocytes.errors import ConfigError
from astrocytes.pipeline.orchestrator import PipelineOrchestrator
from astrocytes.provider import DocumentStore, GraphStore, LLMProvider, VectorStore

T = TypeVar("T")


def _cfg_dict(cfg: dict[str, str | int | float | bool | None] | None) -> dict[str, Any]:
    if not cfg:
        return {}
    return {k: v for k, v in cfg.items() if v is not None}


def _instantiate(cls: type[T], kwargs: dict[str, Any], label: str) -> T:
    try:
        if kwargs:
            return cls(**kwargs)
        return cls()
    except TypeError as e:
        raise ConfigError(f"Invalid configuration for {label}: {e}") from e


def resolve_vector_store(config: AstrocyteConfig) -> VectorStore:
    name = (
        config.vector_store
        or os.environ.get("ASTROCYTES_VECTOR_STORE")
        or "in_memory"
    )
    try:
        cls = resolve_provider(name, "vector_stores")
    except LookupError as e:
        raise ConfigError(
            f"Vector store {name!r} not found. Install a provider package or use "
            f"a 'package.module:ClassName' path. ({e})"
        ) from e
    return _instantiate(cls, _cfg_dict(config.vector_store_config), f"vector_store {name!r}")


def resolve_llm_provider(config: AstrocyteConfig) -> LLMProvider:
    name = config.llm_provider or os.environ.get("ASTROCYTES_LLM_PROVIDER") or "mock"
    try:
        cls = resolve_provider(name, "llm_providers")
    except LookupError as e:
        raise ConfigError(
            f"LLM provider {name!r} not found. Install a provider package or use "
            f"a 'package.module:ClassName' path. ({e})"
        ) from e
    return _instantiate(cls, _cfg_dict(config.llm_provider_config), f"llm_provider {name!r}")


def resolve_graph_store(config: AstrocyteConfig) -> GraphStore | None:
    name = config.graph_store or os.environ.get("ASTROCYTES_GRAPH_STORE")
    if not name:
        return None
    try:
        cls = resolve_provider(name, "graph_stores")
    except LookupError as e:
        raise ConfigError(f"Graph store {name!r} not found. ({e})") from e
    return _instantiate(cls, _cfg_dict(config.graph_store_config), f"graph_store {name!r}")


def resolve_document_store(config: AstrocyteConfig) -> DocumentStore | None:
    name = config.document_store or os.environ.get("ASTROCYTES_DOCUMENT_STORE")
    if not name:
        return None
    try:
        cls = resolve_provider(name, "document_stores")
    except LookupError as e:
        raise ConfigError(f"Document store {name!r} not found. ({e})") from e
    return _instantiate(cls, _cfg_dict(config.document_store_config), f"document_store {name!r}")


def build_tier1_pipeline(config: AstrocyteConfig) -> PipelineOrchestrator:
    """Construct `PipelineOrchestrator` from config using registered entry points or import paths."""
    if config.provider_tier != "storage":
        raise ConfigError("build_tier1_pipeline requires provider_tier == 'storage'")

    vector_store = resolve_vector_store(config)
    llm = resolve_llm_provider(config)
    graph_store = resolve_graph_store(config)
    document_store = resolve_document_store(config)

    return PipelineOrchestrator(
        vector_store=vector_store,
        llm_provider=llm,
        graph_store=graph_store,
        document_store=document_store,
    )
