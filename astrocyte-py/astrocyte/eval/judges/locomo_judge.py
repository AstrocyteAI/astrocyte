"""Canonical LoCoMo judge — ported from the paper's reference evaluation.

Upstream: ``datasets/locomo/task_eval/evaluation.py`` from
https://github.com/snap-research/locomo. This module reproduces the
scoring logic used in the LoCoMo paper and subsequent public
comparisons (Mem0, Zep, Hindsight). Deterministic, pure Python, no LLM
— cheaper and more reproducible than LLM-judge approaches.

## Scoring model

LoCoMo evaluates QA predictions with **stemmed token-F1** on normalized
text, with **category-specific adjustments**:

- **Category 1 — multi-hop**: prediction and ground truth are each split
  on commas into sub-answers; for each ground-truth sub-answer, take the
  max F1 across all prediction sub-answers; average those maxes.
- **Category 2 — single-hop**: plain stemmed-token F1.
- **Category 3 — temporal**: ground truth may have multiple acceptable
  forms separated by ``;`` — use the first. Plain F1.
- **Category 4 — open-domain**: plain F1.
- **Category 5 — adversarial**: correct if the prediction signals
  abstention (``"no information available"`` or ``"not mentioned"``).
  Binary 1/0 score.

## Normalization pipeline

1. lowercase
2. remove commas
3. remove articles (``a|an|the|and``)
4. remove punctuation
5. collapse whitespace
6. Porter stem each resulting token

F1 is then computed on multiset-intersection of stemmed tokens.

## What this module does NOT do

- Does not generate answers (that's the upstream LLM pass — Astrocyte's
  reflect stage).
- Does not ingest per-question context / evidence checking.
- Does not compute BERTScore or RougeL (unused by the paper's headline
  metric).

Those live upstream in the adapter that calls this judge.
"""

from __future__ import annotations

import re
import string
from collections import Counter
from typing import Final

from astrocyte.eval.judges._stemmer import porter_stem

#: Category id mapping used by the canonical LoCoMo evaluator. Astrocyte's
#: adapter exposes categories as strings (``"multi-hop"``, etc.);
#: callers translate via :func:`locomo_category_id` before scoring.
#:
#: Verified against ``datasets/locomo/data/locomo10.json``:
#:
#: - cat 1 — multi-hop (multi-speaker synthesis, comma-listed answers)
#: - cat 2 — temporal ("when did..." questions)
#: - cat 3 — open-domain (commonsense inference — "would X likely...")
#: - cat 4 — single-hop (single-session factual)
#: - cat 5 — adversarial (unanswerable — empty GT)
LOCOMO_CATEGORY_IDS: Final[dict[str, int]] = {
    "multi-hop": 1,
    "temporal": 2,
    "open-domain": 3,
    "single-hop": 4,
    "adversarial": 5,
}

#: Tokens treated as articles and removed during normalization. Matches
#: the upstream ``normalize_answer`` regex exactly (``a|an|the|and``).
_ARTICLES_RE: Final[re.Pattern[str]] = re.compile(r"\b(a|an|the|and)\b")

#: Punctuation set removed during normalization (Python ``string.punctuation``).
_PUNCTUATION: Final[frozenset[str]] = frozenset(string.punctuation)

#: Abstention signal phrases for category-5 scoring.
#:
#: The upstream paper's list is narrow (``"no information available"`` and
#: ``"not mentioned"``) because its baseline LLMs were instruction-tuned
#: toward that phrasing. Real-world reflect stages produce a wider range
#: of abstention expressions ("not in my memory", "cannot find", etc.)
#: that are semantically equivalent but miss the narrow match. Extend
#: here when operators find false-negatives in their v5+ runs; the tests
#: in :mod:`tests.test_eval_judges` pin the current set.
_ABSTENTION_PHRASES: Final[tuple[str, ...]] = (
    # Upstream canonical phrases
    "no information available",
    "not mentioned",
    # Common LLM-output variants observed in v5 runs
    "no information",
    "not available",
    "not found",
    "not stated",
    "not specified",
    "not provided",
    "not discussed",
    "not indicated",
    "cannot find",
    "can't find",
    "don't have",
    "do not have",
    "unable to find",
    "no record",
    "nothing about",
    "not in the memor",       # prefix: "not in the memory" / "memories"
    "not in my memor",        # prefix: "not in my memory" / "memories"
    "not in the conversation",
    "not in the provided",
    "no mention",
    "isn't mentioned",
    "wasn't mentioned",
    "i don't know",
    "i do not know",
)


# ---------------------------------------------------------------------------
# Normalization — mirrors ``normalize_answer`` upstream
# ---------------------------------------------------------------------------


def _normalize_answer(text: str) -> str:
    """Lowercase, strip commas/articles/punctuation, collapse whitespace.

    Matches upstream ``normalize_answer`` in
    ``datasets/locomo/task_eval/evaluation.py``.
    """
    if text is None:
        return ""
    text = str(text)
    text = text.replace(",", "")
    # lower
    text = text.lower()
    # remove punctuation
    text = "".join(ch for ch in text if ch not in _PUNCTUATION)
    # remove articles
    text = _ARTICLES_RE.sub(" ", text)
    # collapse whitespace
    text = " ".join(text.split())
    return text


def _normalize_and_stem(text: str) -> list[str]:
    """Normalize ``text`` and return the list of Porter-stemmed tokens."""
    normalized = _normalize_answer(text)
    if not normalized:
        return []
    return [porter_stem(w) for w in normalized.split()]


# ---------------------------------------------------------------------------
# F1 — mirrors ``f1_score`` upstream
# ---------------------------------------------------------------------------


def _f1_score(prediction: str, ground_truth: str) -> float:
    """Stemmed-token F1 between a prediction string and a ground-truth string.

    Matches ``f1_score`` in
    ``datasets/locomo/task_eval/evaluation.py``. Returns 0.0 when either
    side has no tokens or when there is no token overlap.
    """
    pred_tokens = _normalize_and_stem(prediction)
    gt_tokens = _normalize_and_stem(ground_truth)

    common = Counter(pred_tokens) & Counter(gt_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0

    precision = num_same / len(pred_tokens)
    recall = num_same / len(gt_tokens)
    return (2.0 * precision * recall) / (precision + recall)


def _multi_hop_f1(prediction: str, ground_truth: str) -> float:
    """Multi-hop F1 — for each ground-truth sub-answer, max across all
    prediction sub-answers; then average.

    Both sides split on ``,``. Matches ``f1`` (the multi-answer variant)
    upstream.
    """
    predictions = [p.strip() for p in prediction.split(",") if p.strip()]
    ground_truths = [g.strip() for g in ground_truth.split(",") if g.strip()]
    if not predictions or not ground_truths:
        return 0.0
    per_gt: list[float] = []
    for gt in ground_truths:
        per_gt.append(max(_f1_score(pred, gt) for pred in predictions))
    return sum(per_gt) / len(per_gt)


# ---------------------------------------------------------------------------
# Category-specific dispatch — mirrors ``eval_question_answering`` upstream
# ---------------------------------------------------------------------------


def locomo_category_id(category: str | int) -> int:
    """Translate Astrocyte's string-category to the paper's integer id.

    Accepts either the string form (``"single-hop"``) or the integer
    form (``2``). Raises :class:`ValueError` for unknown categories so a
    regression in adapter naming fails loudly.
    """
    if isinstance(category, int):
        if category in LOCOMO_CATEGORY_IDS.values():
            return category
        raise ValueError(f"Unknown LoCoMo category id: {category!r}")
    key = str(category).strip().lower()
    try:
        return LOCOMO_CATEGORY_IDS[key]
    except KeyError as exc:
        known = ", ".join(sorted(LOCOMO_CATEGORY_IDS.keys()))
        raise ValueError(
            f"Unknown LoCoMo category: {category!r} (known: {known})",
        ) from exc


def locomo_score_qa(
    prediction: str,
    ground_truth: str,
    category: str | int,
) -> float:
    """Score a single LoCoMo QA pair using the canonical judge.

    Returns a float in ``[0.0, 1.0]``. The aggregator (adapter code)
    converts scores to a pass/fail at whatever threshold it wants; the
    paper reports raw means of these F1 scores per category, which we
    match.

    Category-specific semantics:

    - 1 / multi-hop: F1 on split sub-answers; average of per-GT-max.
    - 2 / single-hop: plain F1.
    - 3 / temporal: ground truth may carry alternates separated by ``;``;
      upstream takes the first alternate before scoring. Plain F1.
    - 4 / open-domain: plain F1.
    - 5 / adversarial: 1.0 when prediction contains an abstention
      phrase; 0.0 otherwise.
    """
    cid = locomo_category_id(category)
    if prediction is None:
        prediction = ""
    if ground_truth is None:
        ground_truth = ""

    if cid == 5:  # adversarial
        lower = prediction.lower()
        return 1.0 if any(p in lower for p in _ABSTENTION_PHRASES) else 0.0

    if cid == 1:  # multi-hop
        return _multi_hop_f1(prediction, ground_truth)

    if cid == 3:  # open-domain — upstream takes first alternate
        # Upstream defensive: some open-domain answers carry ``;``-
        # separated alternates. Take only the first; plain F1 after.
        gt_for_scoring = ground_truth.split(";")[0].strip()
        return _f1_score(prediction, gt_for_scoring)

    # cid in {2 temporal, 4 single-hop} — plain F1
    return _f1_score(prediction, ground_truth)


# Normalized-text helpers exposed for adapters that want to display
# what was actually scored (useful in debug logs / per-question reports).


def normalized_for_scoring(text: str) -> str:
    """Return the normalized form used for scoring — articles stripped,
    lowercase, punctuation removed, whitespace collapsed. Stems are NOT
    applied here (stemming is token-level during F1)."""
    return _normalize_answer(text)
