"""Build a fully wired ``Astrocyte`` for the AML adapter.

``Astrocyte.from_config()`` deliberately does not construct a pipeline — it
returns a brain whose ``_pipeline`` is ``None``, and the first ``retain()``
raises ``ConfigError("No provider or pipeline configured")``. The gateway
supplies its own wiring (``astrocyte_gateway.wiring.build_tier1_pipeline``);
without an equivalent, the adapter's ``ASTROCYTE_CONFIG_PATH`` fallback would
start cleanly and then fail on AML's first ``/add``.

This module is that equivalent, kept deliberately small: the adapter needs a
vector store and an LLM provider, and nothing else. Depending on the gateway
package instead would drag its whole HTTP stack into a submission image that
maintainers have to build and run.

Everything resolves through the same entry-point SPI the rest of Astrocyte
uses (``astrocyte.vector_stores`` / ``astrocyte.llm_providers``), so the
container honours ``astrocyte.yaml`` exactly like any other deployment.
"""

from __future__ import annotations

import os
from typing import Any

from astrocyte import Astrocyte, resolve_provider
from astrocyte.config import AstrocyteConfig, load_config
from astrocyte.errors import ConfigError
from astrocyte.pipeline.orchestrator import PipelineOrchestrator


def _instantiate(name: str, group: str, cfg: dict[str, Any] | None) -> Any:
    cls = resolve_provider(name, group)
    try:
        return cls(**(cfg or {}))
    except TypeError as exc:  # pragma: no cover - surfaced as a clear config error
        raise ConfigError(f"Could not construct {group} {name!r}: {exc}") from exc


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    return {k: v for k, v in vars(value).items() if not k.startswith("_")}


def build_pipeline(config: AstrocyteConfig) -> PipelineOrchestrator:
    """Vector store + LLM provider, resolved from config via the SPI."""
    if not config.vector_store:
        raise ConfigError("astrocyte.yaml must set `vector_store` (e.g. postgres).")
    if not config.llm_provider:
        raise ConfigError("astrocyte.yaml must set `llm_provider` (e.g. openai).")

    vector_store = _instantiate(
        config.vector_store, "vector_stores", _as_dict(config.vector_store_config)
    )
    llm = _instantiate(
        config.llm_provider, "llm_providers", _as_dict(config.llm_provider_config)
    )

    # Same auto-wire as the gateway: PostgresStore satisfies DocumentStore via
    # its tsvector layer, so reusing it enables the keyword retrieval strategy
    # without deploying a second search service.
    document_store = vector_store if hasattr(vector_store, "search_fulltext") else None

    return PipelineOrchestrator(
        vector_store=vector_store,
        llm_provider=llm,
        document_store=document_store,
    )


def build_brain(config_path: str | None = None) -> Astrocyte:
    """A wired brain, ready to serve ``/add`` and ``/search``.

    ``config_path`` defaults to ``ASTROCYTE_CONFIG_PATH``.
    """
    path = config_path or os.environ.get("ASTROCYTE_CONFIG_PATH")
    if not path:
        raise ConfigError(
            "ASTROCYTE_CONFIG_PATH is required. It must point at an astrocyte.yaml "
            "declaring vector_store and llm_provider."
        )
    config = load_config(path)
    brain = Astrocyte.from_config(path)
    brain.set_pipeline(build_pipeline(config))
    return brain
