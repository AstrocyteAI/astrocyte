"""Shared provider resolution — one implementation for every deployment.

Astrocyte is provider-agnostic by design: `astrocyte.yaml` names providers and
the entry-point SPI resolves them. That guarantee only holds if every service
resolves a given config *the same way*, and for a while they did not — the
gateway ignored ``embedding_provider`` while the AML adapter honoured it, so
one YAML file meant different things depending on which process loaded it. A
key that parses and silently does nothing is worse than an unsupported one.

This module is the single resolver. Callers own their own store wiring; what
they share is how an LLM provider is built from config, including the split
completion/embedding case.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from ._discovery import resolve_provider
from .errors import ConfigError

if TYPE_CHECKING:  # pragma: no cover
    from .config import AstrocyteConfig

__all__ = ["config_kwargs", "instantiate_provider", "resolve_llm_provider"]


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
    name = config.llm_provider or os.environ.get("ASTROCYTE_LLM_PROVIDER") or "mock"
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
