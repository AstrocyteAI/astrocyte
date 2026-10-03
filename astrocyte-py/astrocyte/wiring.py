"""Shared provider resolution — one implementation for every deployment.

Astrocyte is provider-agnostic by design: `astrocyte.yaml` names providers and
the entry-point SPI resolves them. That guarantee only holds if every service
resolves a given config *the same way*, and for a while they did not — the
gateway ignored ``embedding_provider`` while the AML adapter honoured it, so
one YAML file meant different things depending on which process loaded it. A
key that parses and silently does nothing is worse than an unsupported one.

This module is the single resolver for providers *and* stores. Store wiring
used to be left to each caller, and the same failure recurred one level up:
the gateway and AML adapter each grew their own copy, while ``astrocyte-mcp``
— the entry point coding agents actually launch — got none, so every
``memory_retain`` it served returned ``stored: false`` under any config.
:func:`build_astrocyte` is now the one way to turn a config into a working
brain.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from ._discovery import resolve_provider
from .errors import ConfigError

if TYPE_CHECKING:  # pragma: no cover
    from .config import AstrocyteConfig

logger = logging.getLogger("astrocyte.wiring")

__all__ = [
    "build_astrocyte",
    "build_pipeline",
    "config_kwargs",
    "instantiate_provider",
    "resolve_llm_provider",
    "resolve_store",
    "wants_storage_pipeline",
    "wire_astrocyte",
]

# Config key -> entry-point group. Each store is named by ``<key>`` and
# configured by ``<key>_config``, with ``ASTROCYTE_<KEY>`` as the env fallback.
_STORE_GROUPS = {
    "vector_store": "vector_stores",
    "graph_store": "graph_stores",
    "document_store": "document_stores",
    "wiki_store": "wiki_stores",
    "mental_model_store": "mental_model_stores",
    "source_store": "source_stores",
}


def config_kwargs(cfg: Any) -> dict[str, Any]:
    """Provider config as kwargs, dropping unset keys.

    ``None`` values are stripped so a partially-specified block falls through
    to the provider's own defaults rather than overriding them with ``None``.
    """
    if cfg is None:
        return {}
    if not isinstance(cfg, dict):
        cfg = {k: v for k, v in vars(cfg).items() if not k.startswith("_")}
    return {k: v for k, v in cfg.items() if v is not None}


def instantiate_provider(name: str, group: str, cfg: Any = None, *, label: str | None = None) -> Any:
    """Resolve ``name`` in entry-point ``group`` and construct it."""
    what = label or f"{group} {name!r}"
    try:
        cls = resolve_provider(name, group)
    except LookupError as exc:
        raise ConfigError(
            f"{what} not found. Install a provider package or use a 'package.module:ClassName' path. ({exc})"
        ) from exc
    kwargs = config_kwargs(cfg)
    try:
        return cls(**kwargs) if kwargs else cls()
    except TypeError as exc:
        raise ConfigError(f"Invalid configuration for {what}: {exc}") from exc


def resolve_llm_provider(config: AstrocyteConfig) -> Any:
    """The LLM provider for ``config``, composing a split backend if configured.

    When ``embedding_provider`` is set it is resolved separately and paired
    with the completion provider through :class:`CompositeLLMProvider`. This is
    not a niche path: some completion providers cannot embed at all —
    ``ClaudeCliProvider`` raises ``NotImplementedError`` from ``embed()`` — so
    ignoring the key means ``retain()`` fails at the embedding step with no
    indication that the configured embedder was never consulted.
    """
    name = config.llm_provider or os.environ.get("ASTROCYTE_LLM_PROVIDER")
    if not name:
        # Kept as a default for tests and dev servers, but never silently: mock
        # embeddings make every retain "succeed" and every recall return noise.
        logger.warning(
            "No llm_provider configured; using the mock provider. Memories will not be "
            "meaningfully recalled. Set llm_provider in astrocyte.yaml (or run `astrocyte setup`)."
        )
        name = "mock"
    completion = instantiate_provider(name, "llm_providers", config.llm_provider_config, label=f"llm_provider {name!r}")

    embedder_name = getattr(config, "embedding_provider", None)
    if not embedder_name or embedder_name == name:
        # Same name means one provider serves both roles; composing it with
        # itself would only add a layer of indirection.
        return completion

    from .providers.composite import CompositeLLMProvider

    embedder = instantiate_provider(
        embedder_name,
        "llm_providers",
        getattr(config, "embedding_provider_config", None),
        label=f"embedding_provider {embedder_name!r}",
    )
    return CompositeLLMProvider(completion_provider=completion, embedding_provider=embedder)


def resolve_store(config: AstrocyteConfig, kind: str) -> Any | None:
    """The configured store of ``kind`` (e.g. ``"vector_store"``), or None.

    Resolved from ``config.<kind>`` or ``ASTROCYTE_<KIND>``, constructed with
    ``config.<kind>_config``.
    """
    group = _STORE_GROUPS.get(kind)
    if group is None:
        raise ValueError(f"unknown store kind {kind!r}; expected one of {sorted(_STORE_GROUPS)}")
    name = getattr(config, kind, None) or os.environ.get(f"ASTROCYTE_{kind.upper()}")
    if not name:
        return None
    return instantiate_provider(name, group, getattr(config, f"{kind}_config", None), label=f"{kind} {name!r}")


def build_pipeline(config: AstrocyteConfig, *, wiki_store: Any | None = None, **overrides: Any) -> Any:
    """A storage-tier :class:`PipelineOrchestrator` built from ``config``.

    A vector store is required, and deliberately has no silent default: an
    unconfigured memory server that falls back to an in-process store reports
    every retain as a success and forgets it all on restart.

    ``overrides`` are passed to the orchestrator for callers with their own
    latency contract — e.g. the agent hooks disable recall-time query
    expansion, an LLM call that costs 5–9 s through a CLI provider.
    """
    from .pipeline.entity_resolution import EntityResolver
    from .pipeline.orchestrator import PipelineOrchestrator

    vector_store = resolve_store(config, "vector_store")
    if vector_store is None:
        raise ConfigError(
            "No vector_store configured. Run `astrocyte setup` for a local SQLite store, "
            "or set `vector_store` in astrocyte.yaml (e.g. sqlite, postgres)."
        )
    llm = resolve_llm_provider(config)
    graph_store = resolve_store(config, "graph_store")
    document_store = resolve_store(config, "document_store")
    # A vector store that also implements DocumentStore (Postgres via tsvector,
    # SQLite via FTS5) enables the keyword retrieval leg with no extra config.
    if document_store is None and hasattr(vector_store, "search_fulltext"):
        document_store = vector_store

    entity_resolver = None
    if config.entity_resolution.enabled:
        if graph_store is None:
            raise ConfigError("entity_resolution.enabled requires a graph_store provider")
        entity_resolver = EntityResolver(
            similarity_threshold=config.entity_resolution.similarity_threshold,
            confirmation_threshold=config.entity_resolution.confirmation_threshold,
            max_candidates_per_entity=config.entity_resolution.max_candidates_per_entity,
        )

    return PipelineOrchestrator(
        vector_store=vector_store,
        llm_provider=llm,
        graph_store=graph_store,
        document_store=document_store,
        wiki_store=wiki_store,
        entity_resolver=entity_resolver,
        **overrides,
    )


def wants_storage_pipeline(config: AstrocyteConfig) -> bool:
    """Does ``config`` name a vector store (in the file or via env)?

    That is the signal a caller expects a working storage-tier brain; configs
    without one (engine tier, or a caller wiring stores by hand) are left as is.
    """
    return bool(getattr(config, "vector_store", None) or os.environ.get("ASTROCYTE_VECTOR_STORE"))


def build_astrocyte(config: AstrocyteConfig) -> Any:
    """A fully wired :class:`Astrocyte`: pipeline plus every configured store."""
    from ._astrocyte import Astrocyte

    return wire_astrocyte(Astrocyte(config), config)


def wire_astrocyte(brain: Any, config: AstrocyteConfig) -> Any:
    """Attach the pipeline and every configured store to ``brain``.

    Optional stores (wiki, mental model, source) are attached only when
    configured, matching the gateway's behaviour, so their tools report
    "not configured" rather than failing obscurely.
    """
    from .config import access_grants_for_astrocyte

    config.provider_tier = "storage"
    wiki_store = resolve_store(config, "wiki_store")
    pipeline = build_pipeline(config, wiki_store=wiki_store)
    brain.set_pipeline(pipeline)

    if wiki_store is not None:
        brain.set_wiki_store(wiki_store)
        if config.wiki_compile.auto_start:
            from .pipeline.compile import CompileEngine
            from .pipeline.compile_trigger import CompileQueue, CompileTriggerConfig

            engine = CompileEngine(
                vector_store=pipeline.vector_store,
                llm_provider=pipeline.llm_provider,
                wiki_store=wiki_store,
            )
            brain.set_compile_queue(
                CompileQueue(
                    engine,
                    CompileTriggerConfig(
                        size_threshold=config.wiki_compile.size_threshold,
                        staleness_days=config.wiki_compile.staleness_days,
                        staleness_min_memories=config.wiki_compile.staleness_min_memories,
                    ),
                    max_queue_size=config.wiki_compile.max_queue_size,
                )
            )
    if (mental_model_store := resolve_store(config, "mental_model_store")) is not None:
        brain.set_mental_model_store(mental_model_store)
    if (source_store := resolve_store(config, "source_store")) is not None:
        brain.set_source_store(source_store)
    if grants := access_grants_for_astrocyte(config):
        brain.set_access_grants(grants)
    return brain
