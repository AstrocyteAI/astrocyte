"""Embedding generation — calls LLM Provider SPI.

Async (I/O-bound). See docs/_design/built-in-pipeline.md section 2.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

logger = logging.getLogger("astrocyte.pipeline")


async def generate_embeddings(
    texts: list[str],
    llm_provider: LLMProvider,
    model: str | None = None,
) -> list[list[float]]:
    """Generate embeddings for a list of texts via the LLM SPI.

    Falls back to a simple hash-based embedding if the provider raises NotImplementedError.
    """
    try:
        return await llm_provider.embed(texts, model=model)
    except NotImplementedError:
        logger.error(
            "Embedding provider does not support embed() — falling back to pseudo-embeddings. "
            "Retrieval quality will be severely degraded. Configure a real embedding provider for production use."
        )
        return [_pseudo_embedding(text) for text in texts]


def _pseudo_embedding(text: str, dims: int = 128) -> list[float]:
    """Generate a deterministic pseudo-embedding from text.

    NOT for production — only for development when no embedding model is available.
    Uses character-level hashing to produce a normalized vector.
    """
    import hashlib

    h = hashlib.sha256(text.encode()).digest()
    raw = [float(b) / 255.0 for b in h]
    # Extend to desired dimensions by repeating
    while len(raw) < dims:
        h = hashlib.sha256(h).digest()
        raw.extend(float(b) / 255.0 for b in h)
    raw = raw[:dims]
    # Normalize
    import math

    norm = math.sqrt(sum(x * x for x in raw))
    if norm > 0:
        raw = [x / norm for x in raw]
    return raw
