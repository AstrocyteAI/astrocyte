"""Multi-query expansion for complex (multi-hop) recall.

When a question requires evidence from multiple sessions or topics,
a single recall query often misses half the evidence. Decomposing
into 2–3 focused sub-questions and merging their recall results
substantially improves multi-hop coverage.

This module handles decomposition only. The orchestrator owns the
recall-and-merge loop so it can reuse the full retrieval pipeline
(embeddings, parallel strategies, RRF, rerank) for each sub-query.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from astrocyte.types import Message

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

_logger = logging.getLogger(__name__)

_DECOMPOSITION_SYSTEM = (
    "You decompose complex questions into simpler sub-questions for memory search. "
    "Return ONLY the sub-questions, one per line, no numbering, no explanation. "
    "If the question is already simple (a single fact lookup), return just the original question unchanged."
)

_DECOMPOSITION_USER = (
    "Decompose this question into 2–3 focused sub-questions whose answers "
    "together answer the original:\n\n{query}"
)

# Hard cap on sub-questions to bound downstream recall cost.
# 3 sub-questions + the original = 4 total recall passes at most.
_MAX_SUB_QUESTIONS = 4


async def decompose_query(query: str, llm_provider: LLMProvider) -> list[str]:
    """Return a list of sub-questions for multi-hop query expansion.

    The first element is always the original query (used as an anchor
    so callers can detect a no-op: ``len(result) == 1`` means the LLM
    judged the question already simple). Capped at ``_MAX_SUB_QUESTIONS``
    total entries.

    Failures (LLM errors, empty responses) return ``[query]`` and log at
    DEBUG so a misconfigured provider degrades to normal single-query
    recall rather than crashing.
    """
    try:
        completion = await llm_provider.complete(
            messages=[
                Message(role="system", content=_DECOMPOSITION_SYSTEM),
                Message(role="user", content=_DECOMPOSITION_USER.format(query=query)),
            ],
            max_tokens=200,
            temperature=0.0,
        )
        lines = [line.strip() for line in completion.text.strip().splitlines() if line.strip()]
    except Exception:
        _logger.debug("Query decomposition failed; falling back to single-query recall", exc_info=True)
        return [query]

    if not lines:
        return [query]

    # Deduplicate while preserving order; original query is always the anchor.
    seen: set[str] = set()
    result: list[str] = []
    for line in [query] + lines:
        norm = line.lower().strip()
        if norm not in seen:
            seen.add(norm)
            result.append(line)

    return result[:_MAX_SUB_QUESTIONS]
