"""Local embeddings LLMProvider — no API, no key.

Embed-only provider backed by a local model, run through either of two
backends that produce the same vectors:

- **fastembed** (ONNX Runtime) — ~143 MB installed, no torch. Preferred.
- **sentence-transformers** (torch) — ~800 MB on macOS and several GB on
  Linux, where the default torch wheel pulls CUDA libraries.

``backend="auto"`` (default) uses fastembed when installed and falls back to
sentence-transformers. For the default ``BAAI/bge-small-en-v1.5`` the two agree
to cosine 1.00000 on English, code and CJK text, with identical rankings even
when the query is embedded by one backend and the corpus by the other — so a
store built under either stays searchable under both.

Usage programmatically::

    from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

    embedder = LocalEmbeddingsProvider()          # BAAI/bge-small-en-v1.5
    vecs = await embedder.embed(["hello world"])  # 1536-dim (zero-padded)

Design notes:

- **``pad_to=1536`` by default** — the Postgres reference schema declares
  ``vector(1536)`` on ``summary_embedding`` / fact embeddings (sized for
  OpenAI ``text-embedding-3-small``). Zero-padding a normalized vector
  preserves cosine similarity ordering exactly, so a 384-dim local model
  drops into the existing DDL with no migration. Vectors longer than
  ``pad_to`` are rejected loudly (truncation would silently change geometry).
- The model loads lazily in a worker thread on first ``embed()`` and is
  cached for the provider's lifetime. First call downloads the model from
  the HuggingFace hub (~130 MB for bge-small) unless already cached.
- ``complete()`` is intentionally unsupported — compose with a completion
  provider via :class:`~astrocyte.providers.composite.CompositeLLMProvider`.

Requires ``fastembed`` (``pip install 'astrocyte[local]'``) or
``sentence-transformers`` (``pip install 'astrocyte[rerank]'``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any, ClassVar

from astrocyte.types import Completion, LLMCapabilities, Message, ToolDefinition

logger = logging.getLogger("astrocyte.providers.local_embeddings")

_MAX_CHARS = 28_000  # parity with OpenAIProvider.embed truncation guard

_BACKENDS = ("fastembed", "sentence-transformers")


def _installed(module: str) -> bool:
    # A module already imported (or injected, e.g. a test stub whose
    # __spec__ is None, which makes find_spec raise) counts as installed.
    if module in sys.modules:
        return sys.modules[module] is not None
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _resolve_backend(requested: str) -> str:
    if requested not in ("auto", *_BACKENDS):
        raise ValueError(f"local_embeddings: backend must be 'auto' or one of {_BACKENDS}, got {requested!r}")
    module = {"fastembed": "fastembed", "sentence-transformers": "sentence_transformers"}
    if requested != "auto":
        if not _installed(module[requested]):
            raise ImportError(
                f"local_embeddings backend {requested!r} is not installed. "
                f"Install it with: pip install {requested}"
            )
        return requested
    for name in _BACKENDS:
        if _installed(module[name]):
            return name
    raise ImportError(
        "LocalEmbeddingsProvider needs an embedding backend. Install the light one with "
        "pip install 'astrocyte[local]' (fastembed), or pip install 'astrocyte[rerank]' "
        "(sentence-transformers)."
    )


def _fastembed_cache_dir() -> str:
    """fastembed defaults to the system temp dir, which macOS and many Linux
    distros clear periodically — silently re-downloading the model. Keep it
    with other user caches unless the operator chose a location."""
    if explicit := os.environ.get("FASTEMBED_CACHE_PATH"):
        return explicit
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return str(Path(base).expanduser() / "astrocyte" / "fastembed")


class LocalEmbeddingsProvider:
    """Embed-only LLMProvider backed by a local embedding model."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-small-en-v1.5",
        pad_to: int | None = 1536,
        device: str | None = None,
        batch_size: int = 64,
        backend: str = "auto",
    ) -> None:
        self._backend = _resolve_backend(backend)
        self._model_name = model_name
        self._pad_to = pad_to
        self._device = device
        self._batch_size = batch_size
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            supports_multimodal_completion=False,
            modalities_supported=("text",),
            supports_multimodal_embedding=False,
            supports_batch_embed=True,
        )

    @property
    def backend(self) -> str:
        return self._backend

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                def _load() -> Any:
                    logger.info(
                        "local_embeddings: loading %s via %s (device=%s)",
                        self._model_name, self._backend, self._device or "auto",
                    )
                    if self._backend == "fastembed":
                        from fastembed import TextEmbedding

                        return TextEmbedding(self._model_name, cache_dir=_fastembed_cache_dir())
                    from sentence_transformers import SentenceTransformer

                    return SentenceTransformer(self._model_name, device=self._device)

                self._model = await asyncio.to_thread(_load)
        return self._model

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,  # noqa: ARG002 — single local model
    ) -> list[list[float]]:
        if not texts:
            return []
        model_obj = await self._ensure_model()
        safe = [t[:_MAX_CHARS] if t else " " for t in texts]

        def _encode() -> list[list[float]]:
            if self._backend == "fastembed":
                # Normalised explicitly, matching normalize_embeddings=True
                # below. Pure Python keeps numpy out of the core package; the
                # cost is negligible beside model inference.
                out: list[list[float]] = []
                for vec in model_obj.embed(safe, batch_size=self._batch_size):
                    vals = [float(x) for x in vec]
                    norm = math.sqrt(sum(x * x for x in vals)) or 1.0
                    out.append([x / norm for x in vals])
                return out
            vecs = model_obj.encode(
                safe,
                batch_size=self._batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return [v.tolist() for v in vecs]

        raw = await asyncio.to_thread(_encode)

        # 0 means "native width" as well as None: provider config passes
        # through config_kwargs, which drops None values, so YAML can only
        # express "no padding" as 0.
        if not self._pad_to:
            return raw
        dim = len(raw[0]) if raw else 0
        if dim > self._pad_to:
            raise ValueError(
                f"local_embeddings: model dim {dim} exceeds pad_to={self._pad_to}; "
                "truncation would change similarity geometry — pick a smaller "
                "model or raise pad_to (requires a schema migration)."
            )
        if dim == self._pad_to:
            return raw
        pad = [0.0] * (self._pad_to - dim)
        return [v + pad for v in raw]

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = None,
        response_format: dict | None = None,
    ) -> Completion:
        raise NotImplementedError(
            "LocalEmbeddingsProvider is embed-only. Compose with a completion "
            "provider via CompositeLLMProvider (astrocyte.providers.composite)."
        )
