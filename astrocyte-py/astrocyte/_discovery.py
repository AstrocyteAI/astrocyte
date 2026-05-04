"""Provider discovery via Python entry points (importlib.metadata)."""

from __future__ import annotations

import importlib
import importlib.metadata
from typing import Any

# Entry point group names — providers register under these in pyproject.toml
ENTRY_POINT_GROUPS = {
    "vector_stores": "astrocyte.vector_stores",
    "graph_stores": "astrocyte.graph_stores",
    "document_stores": "astrocyte.document_stores",
    "wiki_stores": "astrocyte.wiki_stores",
    "mental_model_stores": "astrocyte.mental_model_stores",
    "engine_providers": "astrocyte.engine_providers",
    "llm_providers": "astrocyte.llm_providers",
    "outbound_transports": "astrocyte.outbound_transports",
    "ingest_stream_drivers": "astrocyte.ingest_stream_drivers",
    "ingest_poll_drivers": "astrocyte.ingest_poll_drivers",
}


def discover_entry_points(group: str) -> dict[str, Any]:
    """Discover all registered providers for an entry point group.

    Returns a dict of {name: loaded_class} for all installed providers
    in the given group.
    """
    ep_group = ENTRY_POINT_GROUPS.get(group, group)
    result: dict[str, Any] = {}
    for ep in importlib.metadata.entry_points(group=ep_group):
        result[ep.name] = ep.load()
    return result


def resolve_provider(name: str, group: str) -> Any:
    """Resolve a single provider by name from entry points, or by import path.

    If name contains ":" (e.g., "mypackage.module:ClassName"), it's treated
    as a direct import path. Otherwise, it's looked up in entry points.
    """
    if ":" in name:
        # Direct import path
        module_path, class_name = name.rsplit(":", 1)
        module = importlib.import_module(module_path)
        obj = getattr(module, class_name)
        if not isinstance(obj, type):
            raise TypeError(f"'{name}' resolved to {type(obj).__name__}, expected a class")
        return obj

    # Entry point lookup
    ep_group = ENTRY_POINT_GROUPS.get(group, group)
    for ep in importlib.metadata.entry_points(group=ep_group):
        if ep.name == name:
            return ep.load()

    raise LookupError(f"Provider '{name}' not found in entry point group '{ep_group}'")


def available_providers() -> dict[str, dict[str, Any]]:
    """Discover all installed providers across all groups.

    Returns a dict of {group: {name: class}} for all installed providers.
    """
    result: dict[str, dict[str, Any]] = {}
    for group_key, _ in ENTRY_POINT_GROUPS.items():
        providers = discover_entry_points(group_key)
        if providers:
            result[group_key] = providers
    return result
