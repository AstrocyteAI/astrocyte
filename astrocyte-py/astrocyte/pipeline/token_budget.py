"""Token-budgeted recall packing (M35).

Replaces item-count `top_k` caps with token-count `max_tokens` caps on the
recall output. Matches Hindsight's `max_tokens=8192` default (see
``hindsight-api-slim/hindsight_api/engine/memory_engine.py`` and their
benchmark runner).

Why tokens, not items
---------------------

The LLM answerer consumes a fixed-size token window, not a fixed number of
items. Item-count caps (our pre-M35 `top_k`) over-count short facts and
under-count long sections, leading to either wasted context (lots of short
hits) or truncated context (one long fact eats most of the window).

The v015l bench made this concrete: with `final_top_n=50`, LME dropped to
63.3% top_50 because the rank-21-50 candidates added noise. The same
retrieval at `final_top_n=20` (`top_20`) achieved 74.4% — the bigger pool
of items diluted answerer attention. A token-budget cap behaves
proportionally: short, dense facts pack more in; long sections fill the
budget faster.

Tokenizer
---------

Uses ``tiktoken``'s ``cl100k_base`` encoding by default — the tokenizer
shared by ``gpt-4o``, ``gpt-4o-mini``, ``gpt-4-turbo``, and
``text-embedding-3-*``. This matches what our bench answerer (``gpt-4o-mini``)
actually uses, so the budget is measured in the units that matter.

Tiktoken's first-load cost is ~50ms; the encoding is then cached for the
process lifetime. Subsequent calls are sub-millisecond per text.

References:

- Hindsight: ``hindsight_api/engine/memory_engine.py`` uses
  ``_get_tiktoken_encoding()`` for the same purpose.
- OpenAI tiktoken: https://github.com/openai/tiktoken
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Iterable, TypeVar

_logger = logging.getLogger("astrocyte.pipeline.token_budget")


#: Default encoding shared by all current OpenAI models we care about.
#: Override per-call if a non-OpenAI model is being targeted.
_DEFAULT_ENCODING_NAME: str = "cl100k_base"

#: Lazy-loaded encoding handle. ``tiktoken.get_encoding`` does a small
#: amount of work on first call (~50ms) and caches internally, but we add
#: our own module-level cache so the lookup is sub-microsecond after the
#: first ``count_tokens`` call.
_encoding_cache: dict[str, object] = {}
_encoding_lock = Lock()


def _get_encoding(encoding_name: str = _DEFAULT_ENCODING_NAME):
    """Return a cached tiktoken encoding handle.

    Threadsafe. Lazy-loaded so a process that never calls
    :func:`count_tokens` doesn't pay the tiktoken import cost.
    """
    with _encoding_lock:
        cached = _encoding_cache.get(encoding_name)
        if cached is None:
            import tiktoken  # noqa: PLC0415

            cached = tiktoken.get_encoding(encoding_name)
            _encoding_cache[encoding_name] = cached
        return cached


def count_tokens(text: str, *, encoding_name: str = _DEFAULT_ENCODING_NAME) -> int:
    """Return the number of tokens in ``text`` per the chosen encoding.

    Defaults to ``cl100k_base`` (gpt-4o family). Empty / None text counts
    as zero — callers don't need to guard.
    """
    if not text:
        return 0
    enc = _get_encoding(encoding_name)
    return len(enc.encode(text))


_T = TypeVar("_T")


def pack_to_budget(
    items: Iterable[_T],
    *,
    max_tokens: int,
    text_of: callable,  # type: ignore[type-arg]  # callable[[_T], str]
    encoding_name: str = _DEFAULT_ENCODING_NAME,
) -> list[_T]:
    """Pack items into a token-budgeted output list.

    Iterates ``items`` in their input order (caller should pre-sort by
    relevance), tokenizes each via ``text_of(item)``, and stops accepting
    items once the cumulative token count would exceed ``max_tokens``.

    The first item is always included even if it exceeds the budget on
    its own — otherwise a single long fact would produce an empty
    result, which is the worst possible failure mode for recall.
    Subsequent oversize items are skipped (the budget is a soft cap,
    not a hard truncation of mid-item text).

    Args:
        items: Ranked candidate iterable. Order matters — items are
            packed greedily, so put the most relevant first.
        max_tokens: Token budget. Must be ``> 0``. When ``<= 0``, returns
            an empty list (consistent with "no budget = no output").
        text_of: Callable mapping each item to its token-counted text.
            For ``PageIndexFact`` this is typically
            ``lambda f: f.text or ""``.
        encoding_name: Override the tokenizer (default cl100k_base).

    Returns:
        List of items whose cumulative ``text_of(item)`` token count is
        at most ``max_tokens`` (with the always-include-first rule).
    """
    if max_tokens <= 0:
        return []

    out: list[_T] = []
    used = 0
    for i, item in enumerate(items):
        text = text_of(item) or ""
        cost = count_tokens(text, encoding_name=encoding_name)
        # First item always in (avoid empty result on oversized single fact).
        if i == 0:
            out.append(item)
            used += cost
            continue
        if used + cost > max_tokens:
            continue
        out.append(item)
        used += cost
    return out


__all__ = ["count_tokens", "pack_to_budget"]
