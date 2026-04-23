"""Porter stemmer wrapper for the canonical LoCoMo judge.

Uses ``snowballstemmer`` when available (the ``eval`` extra installs it).
Falls back to a no-op identity stemmer when absent — tests that pin the
exact LoCoMo scores will fail under the fallback, which is the right
signal: if you don't have the stemmer you don't get canonical scores.

Why not just import snowballstemmer directly:
- Keeps snowballstemmer an optional extra (benchmark-only), so general
  users don't pay for a transitive dep they never invoke.
- Gives a clear error path when the extra is missing but the judge is
  called — better than a raw ``ModuleNotFoundError`` from deep in the
  F1 loop.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Protocol

_logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_stemmer() -> "_Stemmer | None":
    try:
        import snowballstemmer
    except ImportError:
        _logger.warning(
            "snowballstemmer not installed; LoCoMo judge will NOT produce "
            "canonical scores. Install with: pip install 'astrocyte[eval]' "
            "(or: pip install snowballstemmer>=2.2).",
        )
        return None
    return snowballstemmer.stemmer("english")


class _Stemmer(Protocol):  # protocol stub for type hints
    def stemWord(self, word: str) -> str:  # pragma: no cover
        """Stem ``word`` and return the stem."""


def porter_stem(word: str) -> str:
    """Porter-stem ``word`` using snowballstemmer.

    Raises :class:`ImportError` when the optional ``eval`` extra is not
    installed. Canonical LoCoMo F1 scores are wrong without stemming, so
    silent degradation is worse than a loud failure.
    """
    stemmer = _get_stemmer()
    if stemmer is None:
        raise ImportError(
            "snowballstemmer is required for canonical LoCoMo scoring. "
            "Install with: pip install 'astrocyte[eval]' "
            "(or: pip install snowballstemmer>=2.2).",
        )
    return stemmer.stemWord(word)
