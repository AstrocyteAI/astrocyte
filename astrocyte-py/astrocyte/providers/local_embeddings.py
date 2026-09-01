"""Local embeddings LLMProvider — sentence-transformers, no API, no key.

Embed-only provider backed by a local sentence-transformers model. Fulfils the
"astrocyte-embed zero-config local mode" carry-forward (v0.15.0 ship-decision
§5) at minimum-viable scope: one model, one process, MPS/CPU auto-select.

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

Requires the ``sentence-transformers`` package (``pip install
'astrocyte[rerank]'`` pulls it, or install directly).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar

from astrocyte.types import Completion, LLMCapabilities, Message, ToolDefinition

logger = logging.getLogger("astrocyte.providers.local_embeddings")

_MAX_CHARS = 28_000  # parity with OpenAIProvider.embed truncation guard


class LocalEmbeddingsProvider:
    """Embed-only LLMProvider backed by a local sentence-transformers model."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-small-en-v1.5",
        pad_to: int | None = 1536,
        device: str | None = None,
        batch_size: int = 64,
    ) -> None:
        try:
            import sentence_transformers  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "LocalEmbeddingsProvider requires 'sentence-transformers'. "
                "Install with: pip install 'astrocyte[rerank]'"
            ) from e
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

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                def _load() -> Any:
                    from sentence_transformers import SentenceTransformer

                    logger.info(
                        "local_embeddings: loading %s (device=%s)",
                        self._model_name, self._device or "auto",
                    )
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
        st_model = await self._ensure_model()
        safe = [t[:_MAX_CHARS] if t else " " for t in texts]

        def _encode() -> list[list[float]]:
            vecs = st_model.encode(
                safe,
                batch_size=self._batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return [v.tolist() for v in vecs]

        raw = await asyncio.to_thread(_encode)

        if self._pad_to is None:
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
