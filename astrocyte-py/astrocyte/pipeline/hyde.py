"""Hypothetical Document Embedding (HyDE) — R1 research technique.

Rather than embedding the raw query and searching for similar chunks, HyDE
asks the LLM to generate a *hypothetical answer* first, then embeds that
answer.  Because the hypothetical answer is written in the same style as
stored memories, it sits much closer to relevant chunks in embedding space
than the question does.

References:
  Gao et al. 2022 — "Precise Zero-Shot Dense Retrieval without Relevance Labels"
  https://arxiv.org/abs/2212.10496

Usage:
  hyde_vec = await generate_hyde_vector(query, llm_provider)
  # None on any failure — caller falls back to original query vector.

All failures are logged at DEBUG and return ``None`` so HyDE is never on the
critical path.  The caller should always have the original query vector as a
fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from astrocyte.types import Message

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

logger = logging.getLogger("astrocyte.hyde")

_SYSTEM_PROMPT = (
    "You are a memory retrieval assistant. Given a search query, generate a "
    "single concise hypothetical memory entry that would perfectly answer the "
    "query. Write it as a factual statement in the style of a stored memory — "
    "not as an answer to a question, not as a question itself. Be specific and "
    "concrete. One or two sentences maximum."
)


async def generate_hyde_vector(
    query: str,
    llm_provider: LLMProvider,
) -> list[float] | None:
    """Generate a hypothetical document for *query* and return its embedding.

    Steps:
      1. Ask the LLM to write a hypothetical memory that would answer *query*.
      2. Embed the hypothetical text using the same embedding path as normal
         retain/recall.
      3. Return the embedding vector, or ``None`` on any failure.

    Args:
        query: The natural-language recall query.
        llm_provider: LLM provider used for both generation and embedding.

    Returns:
        Embedding vector of the hypothetical document, or ``None`` if
        generation or embedding fails (so the caller can fall back gracefully).
    """
    # Inline import to avoid circular dependency at module load time.
    from astrocyte.pipeline.embedding import generate_embeddings

    try:
        hypothetical = await _generate_hypothetical(query, llm_provider)
    except Exception as exc:
        logger.debug("HyDE generation failed — falling back to original query: %s", exc)
        return None

    if not hypothetical:
        return None

    try:
        embeddings = await generate_embeddings([hypothetical], llm_provider)
        return embeddings[0] if embeddings else None
    except Exception as exc:
        logger.debug("HyDE embedding failed — falling back to original query: %s", exc)
        return None


async def _generate_hypothetical(query: str, llm_provider: LLMProvider) -> str:
    """Call the LLM to produce a hypothetical memory for *query*."""
    messages = [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(role="user", content=query),
    ]
    response = await llm_provider.complete(messages, max_tokens=150)
    return (response.text or "").strip()
