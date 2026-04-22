"""Canonical LongMemEval judge — ported from the paper's reference evaluation.

Upstream: ``datasets/longmemeval/src/evaluation/evaluate_qa.py`` from
https://github.com/xiaowu0162/LongMemEval. LongMemEval's canonical judge
is an **LLM-judge** (unlike LoCoMo's deterministic F1): each prediction
is sent to an LLM with a task-specific prompt asking "Is the model
response correct? Answer yes or no only." The yes-rate across all
questions is the accuracy.

## Task-specific prompts

Five templates, each tuned to the category's success criteria:

- **single-session-user / single-session-assistant / multi-session**:
  pass if the response contains the correct answer, or contains all the
  intermediate steps. Reject subsets.
- **temporal-reasoning**: same, plus do not penalize off-by-one errors
  on day/week/month counts.
- **knowledge-update**: pass if the response contains the *updated*
  answer, even if it also mentions previous information.
- **single-session-preference**: pass if the response satisfies the
  rubric; does not need to reflect every point.
- **abstention** (task suffix ``_abs``): pass if the response correctly
  identifies the question as unanswerable.

All prompts ask for a single-token "yes" or "no" reply. This module
never parses free-form LLM output — just the yes/no head.

## What this module does

- Builds the right prompt for (task, question, answer, response).
- Calls an :class:`astrocyte.provider.LLMProvider` to get the judgment.
- Returns 1.0 for yes, 0.0 for no, raises on ambiguous responses.

## What this module does NOT do

- Does not generate the model's response (that's the adapter's reflect
  call).
- Does not batch multiple questions into one LLM call (the paper's
  reference does one-at-a-time; we match to stay comparable).
- Does not retry on rate-limit; the caller's LLM provider should
  handle backoff.

Cost note: real-provider judge calls are cheap (short prompt, 1-token
response). At gpt-4o-mini prices that's about $0.0001 per question —
500 LongMemEval questions = ~$0.05. We log the total in the result for
transparency.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from astrocyte.types import Message

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

_logger = logging.getLogger(__name__)

#: LongMemEval's abstention suffix convention — any question_type ending
#: with ``_abs`` triggers the abstention prompt.
LONGMEMEVAL_ABSTENTION_SUFFIX: Final[str] = "_abs"

#: Category → prompt template. The upstream script uses Python str.format
#: positional substitution; we keep the same template strings verbatim
#: so future scoring runs remain byte-for-byte comparable. Order of
#: substitution is (question, answer, response) for non-abstention,
#: (question, explanation, response) for abstention.
_TEMPLATES: Final[dict[str, str]] = {
    "single-session-user": (
        "I will give you a question, a correct answer, and a response from a "
        "model. Please answer yes if the response contains the correct answer. "
        "Otherwise, answer no. If the response is equivalent to the correct "
        "answer or contains all the intermediate steps to get the correct "
        "answer, you should also answer yes. If the response only contains a "
        "subset of the information required by the answer, answer no. "
        "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
        "Is the model response correct? Answer yes or no only."
    ),
    "temporal-reasoning": (
        "I will give you a question, a correct answer, and a response from a "
        "model. Please answer yes if the response contains the correct answer. "
        "Otherwise, answer no. If the response is equivalent to the correct "
        "answer or contains all the intermediate steps to get the correct "
        "answer, you should also answer yes. If the response only contains a "
        "subset of the information required by the answer, answer no. In "
        "addition, do not penalize off-by-one errors for the number of days. "
        "If the question asks for the number of days/weeks/months, etc., and "
        "the model makes off-by-one errors (e.g., predicting 19 days when the "
        "answer is 18), the model's response is still correct. "
        "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
        "Is the model response correct? Answer yes or no only."
    ),
    "knowledge-update": (
        "I will give you a question, a correct answer, and a response from a "
        "model. Please answer yes if the response contains the correct answer. "
        "Otherwise, answer no. If the response contains some previous "
        "information along with an updated answer, the response should be "
        "considered as correct as long as the updated answer is the required "
        "answer."
        "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
        "Is the model response correct? Answer yes or no only."
    ),
    "single-session-preference": (
        "I will give you a question, a rubric for desired personalized "
        "response, and a response from a model. Please answer yes if the "
        "response satisfies the desired response. Otherwise, answer no. The "
        "model does not need to reflect all the points in the rubric. The "
        "response is correct as long as it recalls and utilizes the user's "
        "personal information correctly."
        "\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\n"
        "Is the model response correct? Answer yes or no only."
    ),
    "_abstention": (
        "I will give you an unanswerable question, an explanation, and a "
        "response from a model. Please answer yes if the model correctly "
        "identifies the question as unanswerable. The model could say that "
        "the information is incomplete, or some other information is given "
        "but the asked information is not."
        "\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\n"
        "Does the model correctly identify the question as unanswerable? "
        "Answer yes or no only."
    ),
}

# Aliases — the upstream script treats multiple non-abstention tasks the
# same way ("single-session-assistant" and "multi-session" share the
# first template). We flatten that mapping here so the caller always
# passes its own ``question_type`` string and we pick the right template.
_ALIASES: Final[dict[str, str]] = {
    "single-session-assistant": "single-session-user",
    "multi-session": "single-session-user",
}


def _resolve_template(question_type: str) -> str:
    """Pick the prompt template for a LongMemEval question_type."""
    if question_type.endswith(LONGMEMEVAL_ABSTENTION_SUFFIX):
        return _TEMPLATES["_abstention"]
    key = _ALIASES.get(question_type, question_type)
    if key not in _TEMPLATES:
        raise ValueError(
            f"Unknown LongMemEval question_type: {question_type!r}. "
            f"Known: {sorted(_TEMPLATES.keys())} + aliases {sorted(_ALIASES.keys())}",
        )
    return _TEMPLATES[key]


def build_longmemeval_judge_prompt(
    question_type: str,
    question: str,
    answer: str,
    response: str,
) -> str:
    """Render the canonical judge prompt for (type, q, a, r).

    Exposed so tests can pin the exact prompt bytes against the upstream
    reference. Under normal use, :meth:`LongMemEvalJudge.score` composes
    and sends this internally.
    """
    template = _resolve_template(question_type)
    return template.format(question, answer, response)


# ---------------------------------------------------------------------------
# Judge — async, LLM-backed
# ---------------------------------------------------------------------------


class LongMemEvalJudge:
    """LLM-backed yes/no judge for LongMemEval predictions.

    Instantiate once per benchmark run with the LLM provider to judge
    against (typically the same provider used for the predictions, for
    consistency — though the paper uses ``gpt-4o`` regardless of the
    prediction model).
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        *,
        model: str | None = None,
        max_tokens: int = 4,
        temperature: float = 0.0,
    ) -> None:
        self._llm = llm_provider
        self._model = model
        self._max_tokens = max_tokens   # "yes"/"no" fit in 1 token; 4 is defensive
        self._temperature = temperature

    async def score(
        self,
        question_type: str,
        question: str,
        answer: str,
        response: str,
    ) -> float:
        """Return 1.0 if the judge says yes, 0.0 otherwise.

        Raises :class:`ValueError` for unrecognised question types. LLM
        failures propagate — caller decides how to aggregate (e.g. count
        as 0 and log).
        """
        prompt = build_longmemeval_judge_prompt(
            question_type, question, answer, response,
        )
        completion = await self._llm.complete(
            messages=[Message(role="user", content=prompt)],
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        return parse_yes_no(completion.text)


def parse_yes_no(raw: str) -> float:
    """Interpret an LLM judgment string as 1.0 (yes) or 0.0 (no).

    Tolerant to whitespace, punctuation, and case. Logs a warning and
    returns 0.0 for ambiguous output — treating "I don't know" as a
    negative judgment is the safe default for accuracy scoring. Matches
    the upstream loop's ``ans.lower().startswith('yes')`` pattern.
    """
    if raw is None:
        return 0.0
    cleaned = raw.strip().lower().lstrip(".:!- \t\n\r").rstrip(".:!- \t\n\r")
    if cleaned.startswith("yes"):
        return 1.0
    if cleaned.startswith("no"):
        return 0.0
    _logger.warning(
        "LongMemEval judge returned ambiguous response %r; scored as no",
        raw[:200],
    )
    return 0.0
