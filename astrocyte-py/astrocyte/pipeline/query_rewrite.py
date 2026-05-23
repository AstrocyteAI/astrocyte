"""Query rewriting for retrieval (M38).

Reformulates the user's natural-language question into a search-optimized
form *before* retrieval. The rewritten query is fed to the recall
pipeline; the ORIGINAL question still reaches the answerer so reasoning
context is preserved.

Why
---

Natural questions often have phrasing that doesn't match how facts are
indexed:

- "Which book did I read a week ago?" → search target is "books read",
  date filter is "week ago"
- "How many weeks since I quit smoking?" → search target is "quit
  smoking" event, answer requires date math
- "What is my favourite coffee?" → search target is "coffee preference"

The cheap pattern: one LLM call (gpt-4o-mini, ~300ms, ~$0.0001) maps
the question into a search query optimized for vector similarity +
keyword match. The answerer still sees the original question so it
reasons in the user's voice.

This is *retrieval-side* rewriting, not query expansion. We produce ONE
rewritten string, not N variants. Multi-query expansion (M38b) is a
future extension.

Compared to Hindsight
---------------------

Hindsight doesn't have an explicit query rewriter at retain or query
time. Their `agentic_reflect` loop achieves a similar effect by letting
the LLM call ``recall(sub_query=...)`` iteratively with refined
queries. M38 is the *non-agentic* equivalent — one rewrite, one
retrieval, much cheaper than a full reflect loop. Stacks naturally
with M36 reflect routing (rewrite first, then route).

Reference
---------

- ``docs/_design/m36-reflect-loop.md`` — M36 reflect routing
- ``docs/_design/m34-query-intent-routing.md`` — query intent + analyzer
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

_logger = logging.getLogger("astrocyte.pipeline.query_rewrite")


class _LLMProvider(Protocol):
    async def complete(self, *, messages: list, model: str | None = None, **kwargs: Any) -> Any:
        ...


_REWRITE_SYSTEM_PROMPT = """\
You rewrite user questions into search queries optimized for semantic + keyword retrieval over a personal memory store.

Rules:
1. Extract the core search target (event, entity, preference, fact).
2. Preserve specific names, numbers, dates, and proper nouns.
3. Drop conversational fluff ("did I tell you about", "do you remember when").
4. For "how many/long" questions about durations, focus on the EVENT being asked about (the duration is answer-side, not retrieval-side).
5. For comparison questions ("which X first", "before or after"), keep BOTH entities in the search query.
6. Output one line, no quotes, no markdown, no commentary.

Examples:
Question: "Which book did I finish reading a week ago?"
Rewrite: books I finished reading

Question: "How many weeks ago did I attend the music festival?"
Rewrite: music festival attendance date

Question: "What's my favourite coffee?"
Rewrite: coffee preference favourite

Question: "Did I tell you about my promotion before or after meeting my new manager?"
Rewrite: promotion event and meeting new manager event

Question: "What is my brother's name?"
Rewrite: brother name family
"""


async def rewrite_query(
    question: str,
    *,
    llm_provider: _LLMProvider,
    model: str | None = None,
    timeout_sec: float = 5.0,
) -> str | None:
    """Rewrite ``question`` into a search-optimized query.

    Returns the rewritten string, or ``None`` if the call fails or the
    rewrite is empty / suspiciously short. Caller should fall back to
    the original question on ``None``.

    Single LLM call (~300ms, ~$0.0001 at gpt-4o-mini pricing). Designed
    to be cheap enough to call on every recall.

    Args:
        question: The user's natural question.
        llm_provider: An object with an awaitable ``complete(messages,
            model)`` method matching the in-tree ``LLMProvider`` shape.
        model: Optional model override. Defaults to the provider's
            default (typically gpt-4o-mini in our bench config).
        timeout_sec: Soft timeout; if the call takes longer than this,
            we still wait (no hard kill) — caller can layer asyncio
            timeouts on top if needed.

    Returns:
        Rewritten query string, or ``None`` on failure / empty output.
    """
    import asyncio

    from astrocyte.types import Message  # noqa: PLC0415

    if not question or not question.strip():
        return None
    msg = question.strip()
    try:
        completion = await asyncio.wait_for(
            llm_provider.complete(
                messages=[
                    Message(role="system", content=_REWRITE_SYSTEM_PROMPT),
                    Message(role="user", content=f"Question: {msg}\nRewrite:"),
                ],
                model=model,
                max_tokens=120,
                temperature=0.0,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        _logger.warning("query_rewrite: timeout after %.1fs on question: %s", timeout_sec, msg[:80])
        return None
    except Exception as exc:  # noqa: BLE001
        _logger.warning("query_rewrite: %s on question: %s", type(exc).__name__, msg[:80])
        return None

    rewritten = (getattr(completion, "text", "") or "").strip()
    # Strip a "Rewrite:" prefix if the model echoes it.
    if rewritten.lower().startswith("rewrite:"):
        rewritten = rewritten[len("rewrite:"):].strip()
    # Strip surrounding quotes the model sometimes adds.
    if (rewritten.startswith('"') and rewritten.endswith('"')) or (
        rewritten.startswith("'") and rewritten.endswith("'")
    ):
        rewritten = rewritten[1:-1].strip()
    if not rewritten or len(rewritten) < 3:
        return None
    # Sanity guard: a rewrite that's twice as long as the original is
    # likely a model hallucination — fall back to the original.
    if len(rewritten) > 2 * len(msg) + 100:
        _logger.warning("query_rewrite: suspiciously long output (%dc vs %dc), discarding", len(rewritten), len(msg))
        return None
    return rewritten


__all__ = ["rewrite_query"]
