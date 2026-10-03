"""The ``astrocyte.yaml`` that ``astrocyte setup`` writes for a local install.

Provider choice is detected, never assumed, and every choice is explained in
the file itself so a developer can see *why* their memory uses what it uses:

* **Completions** — the ``claude`` CLI when present (subscription auth, no API
  key; most people running ``astrocyte setup`` for a coding agent have it),
  otherwise OpenAI when ``OPENAI_API_KEY`` is set.
* **Embeddings** — a local model (``local_embeddings``, fastembed preferred)
  at its native 384-dim width; OpenAI's only if no local backend is installed.

Setup never overwrites an existing config: it is the user's file.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class SetupError(Exception):
    """Setup cannot proceed; the message says what to do."""


@dataclass(frozen=True)
class ProviderChoice:
    llm_provider: str
    llm_provider_config: dict[str, Any]
    llm_why: str
    embedding_provider: str | None = None
    embedding_provider_config: dict[str, Any] = field(default_factory=dict)
    embedding_why: str = ""


def _module_available(name: str) -> bool:
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def local_embedding_backend() -> str | None:
    for module, label in (("fastembed", "fastembed"), ("sentence_transformers", "sentence-transformers")):
        if _module_available(module):
            return label
    return None


def choose_providers(env: dict[str, str] | None = None) -> ProviderChoice:
    env = dict(os.environ if env is None else env)
    backend = local_embedding_backend()

    if shutil.which("claude"):
        llm, llm_cfg = "claude_cli", {"model": "haiku"}
        llm_why = "the Claude Code CLI on your PATH (subscription auth, no API key)"
    elif env.get("OPENAI_API_KEY"):
        if not _module_available("openai"):
            raise SetupError(
                "OPENAI_API_KEY is set but the OpenAI SDK isn't installed here. Install it with: "
                "uv tool install --force 'astrocyte[local]' (or pip install 'astrocyte[openai]')."
            )
        llm, llm_cfg = "openai", {"model": "gpt-4o-mini"}
        llm_why = "OpenAI, because OPENAI_API_KEY is set"
    else:
        raise SetupError(
            "No language model available for fact extraction. Either install Claude Code "
            "(https://claude.com/claude-code) so `claude` is on your PATH, or set OPENAI_API_KEY."
        )

    if backend:
        return ProviderChoice(
            llm, llm_cfg, llm_why,
            embedding_provider="local_embeddings",
            # 0 = native width (384 for bge-small): SQLite has no fixed column,
            # and padding to 1536 would cost ~4x in search time and disk.
            embedding_provider_config={"pad_to": 0},
            embedding_why=f"a local bge-small model via {backend} (no API key; nothing leaves this machine)",
        )
    if llm == "openai":
        return ProviderChoice(llm, llm_cfg, llm_why, embedding_why="OpenAI (no local embedding backend installed)")
    raise SetupError(
        "No embedding model available. Install the local one with: pip install 'astrocyte[local]' "
        "(or set OPENAI_API_KEY)."
    )


def _yaml_scalar(value: Any) -> str:
    # JSON scalars are valid YAML and quote paths/specials safely.
    return json.dumps(value)


def _yaml_block(key: str, cfg: dict[str, Any]) -> str:
    if not cfg:
        return ""
    lines = [f"{key}:"] + [f"  {k}: {_yaml_scalar(v)}" for k, v in cfg.items()]
    return "\n".join(lines) + "\n"


def render_config(choice: ProviderChoice, db_path: Path) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    out = [
        f"# Written by `astrocyte setup` on {stamp}. Yours to edit — setup never overwrites it.",
        "# Check it any time with: astrocyte doctor",
        "provider_tier: storage",
        "",
        "# Every memory lives in one local SQLite file. No server to run.",
        "vector_store: sqlite",
        _yaml_block("vector_store_config", {"path": str(db_path)}).rstrip(),
        "",
        f"# Fact extraction uses {choice.llm_why}.",
        f"llm_provider: {choice.llm_provider}",
        _yaml_block("llm_provider_config", choice.llm_provider_config).rstrip(),
    ]
    if choice.embedding_provider:
        out += [
            "",
            f"# Embeddings use {choice.embedding_why}.",
            f"embedding_provider: {choice.embedding_provider}",
            _yaml_block("embedding_provider_config", choice.embedding_provider_config).rstrip(),
        ]
    elif choice.embedding_why:
        out += ["", f"# Embeddings use {choice.embedding_why}."]
    return "\n".join(line for line in out if line is not None) + "\n"


def write_config(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
