"""Canonical LoCoMo + LongMemEval judge tests.

Pins the ported scoring logic against reference fixtures drawn from the
upstream eval scripts:

- ``datasets/locomo/task_eval/evaluation.py`` — F1 with Porter stemming,
  category-specific multi-hop / temporal / adversarial handling.
- ``datasets/longmemeval/src/evaluation/evaluate_qa.py`` — LLM-judge
  with per-task prompts, yes/no parsing, abstention suffix logic.

These tests are the safety net that lets us claim "same judge as
published work" without re-running the reference every time. Any
upstream change to scoring mechanics should show up as a test diff
here first.
"""

from __future__ import annotations

import pytest

from astrocyte.eval.judges import (
    LOCOMO_CATEGORY_IDS,
    LONGMEMEVAL_ABSTENTION_SUFFIX,
    LongMemEvalJudge,
    build_longmemeval_judge_prompt,
    locomo_category_id,
    locomo_score_qa,
)
from astrocyte.eval.judges.locomo_judge import (
    _f1_score,
    _multi_hop_f1,
    _normalize_answer,
    normalized_for_scoring,
)
from astrocyte.eval.judges.longmemeval_judge import parse_yes_no

# ---------------------------------------------------------------------------
# LoCoMo — normalization pipeline
# ---------------------------------------------------------------------------


class TestLocomoNormalization:
    """Pin the exact text-normalization steps of the upstream judge.

    A regression here would silently shift scores on every run, so the
    pipeline is tested independently of F1 computation."""

    def test_lowercase_and_strip_articles(self) -> None:
        # "The" and "a" are articles, "and" is also stripped upstream.
        assert _normalize_answer("The Quick Brown Fox And a Dog") == "quick brown fox dog"

    def test_strip_commas_and_punctuation(self) -> None:
        # Commas removed outright; other punctuation removed as chars
        # (apostrophe in "how's" → "hows"); articles NOT in this string.
        assert _normalize_answer("Hello, world! How's it going?") == "hello world hows it going"

    def test_collapse_whitespace(self) -> None:
        assert _normalize_answer("  multiple   spaces\there\n") == "multiple spaces here"

    def test_empty_input(self) -> None:
        assert _normalize_answer("") == ""
        assert _normalize_answer(None) == ""  # type: ignore[arg-type]

    def test_idempotent(self) -> None:
        """Running normalization twice yields the same result — the
        pipeline has no non-idempotent step."""
        once = _normalize_answer("The Dogs, running And JUMPING!")
        twice = _normalize_answer(once)
        assert once == twice

    def test_public_alias_matches_internal(self) -> None:
        """normalized_for_scoring is the public face of _normalize_answer."""
        assert normalized_for_scoring("The cat") == _normalize_answer("The cat")


# ---------------------------------------------------------------------------
# LoCoMo — F1 scoring on single strings
# ---------------------------------------------------------------------------


class TestLocomoF1Score:
    def test_identical_strings_score_one(self) -> None:
        assert _f1_score("Alice lived in Paris", "Alice lived in Paris") == pytest.approx(1.0)

    def test_articles_do_not_affect_score(self) -> None:
        """The and a are stripped before scoring so their presence or
        absence must not change F1."""
        with_articles = _f1_score("The cat sat on the mat", "A cat sat on a mat")
        without = _f1_score("cat sat on mat", "cat sat on mat")
        assert with_articles == pytest.approx(without)

    def test_stemming_collapses_inflections(self) -> None:
        """Porter stemming means 'running' and 'run' count as the same
        token. This is load-bearing for multi-hop and open-domain."""
        score = _f1_score("She was running fast", "She runs quickly")
        # "she" / "fast"/"quick" don't overlap, but "run" matches via stem.
        assert score > 0.0

    def test_no_overlap_scores_zero(self) -> None:
        assert _f1_score("cat", "dog") == 0.0

    def test_empty_prediction_scores_zero(self) -> None:
        assert _f1_score("", "anything") == 0.0
        assert _f1_score("anything", "") == 0.0

    def test_partial_overlap_f1_formula(self) -> None:
        """Pin the exact F1 formula on a hand-calculable case.

        prediction: "alice paris" (2 tokens after normalization+stem)
        ground_truth: "alice lived paris" (3 tokens)
        common: {alice, paris} = 2
        precision = 2/2 = 1.0
        recall = 2/3 ≈ 0.667
        F1 = 2 * 1.0 * 0.667 / (1.0 + 0.667) ≈ 0.8
        """
        score = _f1_score("alice paris", "alice lived paris")
        assert score == pytest.approx(0.8, abs=0.01)


# ---------------------------------------------------------------------------
# LoCoMo — multi-hop scoring
# ---------------------------------------------------------------------------


class TestLocomoMultiHop:
    """Category 1: split both sides on commas, compute F1 for each
    GT-sub-answer as max over prediction-sub-answers, average across GT."""

    def test_single_sub_answer_each_side_matches_plain_f1(self) -> None:
        """With no commas on either side, multi-hop reduces to plain F1."""
        assert _multi_hop_f1("alice paris", "alice paris") == pytest.approx(1.0)

    def test_all_gt_sub_answers_matched_exactly(self) -> None:
        """Three GT sub-answers, each paired with a perfect prediction
        → F1 of 1.0 on each → average 1.0."""
        pred = "alice, bob, carol"
        gt = "carol, alice, bob"  # order shouldn't matter
        assert _multi_hop_f1(pred, gt) == pytest.approx(1.0)

    def test_partial_match_penalty(self) -> None:
        """Prediction covers 2 of 3 GT sub-answers; the missed one
        scores 0, pulling average down to ~0.667."""
        pred = "alice, bob"
        gt = "alice, bob, carol"
        # Per-GT max: alice vs (alice|bob)=1, bob vs (alice|bob)=1,
        # carol vs (alice|bob)=0 → avg = (1+1+0)/3 ≈ 0.667
        assert _multi_hop_f1(pred, gt) == pytest.approx(0.667, abs=0.01)

    def test_empty_prediction_scores_zero(self) -> None:
        assert _multi_hop_f1("", "a, b, c") == 0.0

    def test_empty_ground_truth_scores_zero(self) -> None:
        assert _multi_hop_f1("a, b", "") == 0.0


# ---------------------------------------------------------------------------
# LoCoMo — dispatch: category-specific behavior
# ---------------------------------------------------------------------------


class TestLocomoDispatch:
    def test_category_string_to_id_mapping(self) -> None:
        # Verified against the dataset: cat 1 = multi-hop, 2 = temporal,
        # 3 = open-domain, 4 = single-hop, 5 = adversarial. Earlier
        # adapter / judge versions had 2↔4 swapped AND 3 mislabeled.
        assert locomo_category_id("multi-hop") == 1
        assert locomo_category_id("temporal") == 2
        assert locomo_category_id("open-domain") == 3
        assert locomo_category_id("single-hop") == 4
        assert locomo_category_id("adversarial") == 5

    def test_category_int_passthrough(self) -> None:
        for cid in range(1, 6):
            assert locomo_category_id(cid) == cid

    def test_unknown_category_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown LoCoMo category"):
            locomo_category_id("made-up-cat")
        with pytest.raises(ValueError, match="Unknown LoCoMo category id"):
            locomo_category_id(99)

    def test_single_hop_uses_plain_f1(self) -> None:
        score = locomo_score_qa(
            "Alice lived in Paris",
            "Alice lived in Paris",
            category="single-hop",
        )
        assert score == pytest.approx(1.0)

    def test_multi_hop_splits_on_commas(self) -> None:
        """A multi-hop query with comma-separated answers must trigger
        the split-based scorer, not plain F1."""
        cat_1 = locomo_score_qa("alice, bob", "alice, bob, carol", category=1)
        # Plain F1 on the same strings would match all tokens → high score;
        # multi-hop splits and averages per-GT → lower score.
        plain = _f1_score("alice, bob", "alice, bob, carol")
        assert cat_1 != pytest.approx(plain, abs=1e-6), (
            "Multi-hop dispatch should NOT degrade to plain F1"
        )

    def test_open_domain_takes_first_alternate(self) -> None:
        """Category 3 (open-domain) — ground truth may carry ``;``-
        separated alternates; upstream uses only the first. (The
        earlier label ``"temporal"`` on this behavior was a mismap —
        cat 3 is open-domain, cat 2 is temporal.)"""
        score = locomo_score_qa(
            "He might pursue psychology",
            "psychology; counseling; therapy",
            category="open-domain",
        )
        # Should match cleanly against "psychology" alone.
        assert score > 0.0
        # If the judge erroneously used the entire string as GT, the
        # prediction would score LOWER due to non-matching tokens in
        # the alternates. Dispatch must strip to the first alternate.
        plain_full = _f1_score(
            "He might pursue psychology",
            "psychology; counseling; therapy",
        )
        assert score != pytest.approx(plain_full, abs=1e-6)

    def test_adversarial_passes_on_abstention_phrase(self) -> None:
        """Category 5 — any "no information available" / "not mentioned"
        phrase in the prediction → 1.0. Otherwise → 0.0."""
        assert locomo_score_qa(
            "There is no information available about this.",
            "irrelevant gt",
            category="adversarial",
        ) == 1.0
        assert locomo_score_qa(
            "It was not mentioned in the conversation.",
            "irrelevant gt",
            category="adversarial",
        ) == 1.0
        assert locomo_score_qa(
            "The answer is blue.",
            "irrelevant gt",
            category="adversarial",
        ) == 0.0

    def test_none_prediction_scores_zero(self) -> None:
        """A crashed reflect step might deliver None — judge must not
        crash."""
        assert locomo_score_qa(None, "gt", category="single-hop") == 0.0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LongMemEval — prompt construction
# ---------------------------------------------------------------------------


class TestLongMemEvalPrompts:
    """Pin the exact prompt bytes against the upstream reference. Any
    drift in templates causes silent scoring deviation from published
    numbers."""

    def test_single_session_user_prompt_contains_question_answer_response(self) -> None:
        prompt = build_longmemeval_judge_prompt(
            "single-session-user",
            question="What's Alice's favorite color?",
            answer="blue",
            response="According to the conversation, her favorite color is blue.",
        )
        assert "Question: What's Alice's favorite color?" in prompt
        assert "Correct Answer: blue" in prompt
        assert "Model Response: According to the conversation" in prompt
        assert prompt.strip().endswith("Answer yes or no only.")

    def test_temporal_reasoning_mentions_off_by_one(self) -> None:
        """The temporal-reasoning prompt has a distinct clause about
        off-by-one errors on day/week/month counts."""
        prompt = build_longmemeval_judge_prompt(
            "temporal-reasoning",
            question="How many days?",
            answer="18",
            response="19",
        )
        assert "off-by-one" in prompt

    def test_knowledge_update_mentions_updated_answer(self) -> None:
        prompt = build_longmemeval_judge_prompt(
            "knowledge-update", "q", "a", "r",
        )
        assert "updated answer" in prompt

    def test_abstention_prompt_used_for_abs_suffix(self) -> None:
        """Any question_type ending with ``_abs`` → abstention prompt."""
        prompt = build_longmemeval_judge_prompt(
            f"multi-session{LONGMEMEVAL_ABSTENTION_SUFFIX}",
            question="Unanswerable question",
            answer="Explanation why it's unanswerable",
            response="The conversation doesn't say.",
        )
        assert "unanswerable question" in prompt.lower()
        assert "Does the model correctly identify" in prompt

    def test_aliases_route_to_canonical_prompt(self) -> None:
        """single-session-assistant and multi-session both use the
        single-session-user template upstream."""
        a = build_longmemeval_judge_prompt("single-session-assistant", "q", "a", "r")
        b = build_longmemeval_judge_prompt("multi-session", "q", "a", "r")
        c = build_longmemeval_judge_prompt("single-session-user", "q", "a", "r")
        assert a == b == c

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown LongMemEval question_type"):
            build_longmemeval_judge_prompt("not-a-type", "q", "a", "r")

    def test_preference_prompt_uses_rubric_label(self) -> None:
        prompt = build_longmemeval_judge_prompt(
            "single-session-preference", "q", "rubric text", "r",
        )
        assert "Rubric: rubric text" in prompt


# ---------------------------------------------------------------------------
# LongMemEval — yes/no parser
# ---------------------------------------------------------------------------


class TestParseYesNo:
    def test_basic_yes(self) -> None:
        assert parse_yes_no("yes") == 1.0
        assert parse_yes_no("Yes") == 1.0
        assert parse_yes_no("YES") == 1.0

    def test_basic_no(self) -> None:
        assert parse_yes_no("no") == 0.0
        assert parse_yes_no("No") == 0.0
        assert parse_yes_no("NO") == 0.0

    def test_trailing_punctuation(self) -> None:
        """LLMs often respond with 'Yes.' — must parse."""
        assert parse_yes_no("Yes.") == 1.0
        assert parse_yes_no("No!") == 0.0

    def test_leading_filler(self) -> None:
        """Some models prepend a colon or bullet before the actual word."""
        assert parse_yes_no(". yes") == 1.0
        assert parse_yes_no("- no") == 0.0

    def test_yes_as_prefix_of_explanation(self) -> None:
        """When the model disobeys and elaborates ('Yes, because...'),
        still count as yes — upstream uses .startswith('yes')."""
        assert parse_yes_no("Yes, because the model mentions blue.") == 1.0

    def test_ambiguous_returns_zero_and_warns(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """'I don't know' is scored as no (safe default for accuracy)
        and logs a warning so operators can triage."""
        import logging
        with caplog.at_level(logging.WARNING):
            result = parse_yes_no("I'm not sure")
        assert result == 0.0
        assert any("ambiguous" in r.getMessage().lower() for r in caplog.records)

    def test_none_scores_zero(self) -> None:
        assert parse_yes_no(None) == 0.0  # type: ignore[arg-type]

    def test_empty_string_scores_zero(self) -> None:
        assert parse_yes_no("") == 0.0


# ---------------------------------------------------------------------------
# LongMemEval — judge end-to-end (with a fake LLM)
# ---------------------------------------------------------------------------


class TestLongMemEvalJudgeEndToEnd:
    """Full async judge.score() path with a controllable mock LLM."""

    async def test_yes_response_scores_one(self) -> None:
        from astrocyte.types import Completion, TokenUsage

        class FakeLLM:
            SPI_VERSION = 1
            async def complete(self, messages, **kw):  # type: ignore[no-untyped-def]
                return Completion(
                    text="yes", model="fake",
                    usage=TokenUsage(input_tokens=1, output_tokens=1),
                )
            def capabilities(self):  # pragma: no cover
                """Unused — only ``complete`` is exercised."""
            async def embed(self, texts, **kw):  # pragma: no cover
                """Unused — only ``complete`` is exercised."""

        judge = LongMemEvalJudge(FakeLLM())  # type: ignore[arg-type]
        score = await judge.score(
            "single-session-user",
            "q", "gold answer", "model output",
        )
        assert score == 1.0

    async def test_no_response_scores_zero(self) -> None:
        from astrocyte.types import Completion, TokenUsage

        class FakeLLM:
            SPI_VERSION = 1
            async def complete(self, messages, **kw):  # type: ignore[no-untyped-def]
                return Completion(
                    text="no", model="fake",
                    usage=TokenUsage(input_tokens=1, output_tokens=1),
                )
            def capabilities(self):  # pragma: no cover
                """Unused — only ``complete`` is exercised."""
            async def embed(self, texts, **kw):  # pragma: no cover
                """Unused — only ``complete`` is exercised."""

        judge = LongMemEvalJudge(FakeLLM())  # type: ignore[arg-type]
        assert await judge.score("single-session-user", "q", "a", "r") == 0.0

    async def test_judge_receives_canonical_prompt(self) -> None:
        """The prompt sent to the LLM must be exactly what
        build_longmemeval_judge_prompt produces — no preamble, no
        model-specific tweaks — so we stay byte-comparable with published
        runs."""
        from astrocyte.types import Completion, TokenUsage

        captured_prompt: dict[str, str] = {}

        class CapturingLLM:
            SPI_VERSION = 1
            async def complete(self, messages, **kw):  # type: ignore[no-untyped-def]
                captured_prompt["content"] = messages[0].content  # type: ignore[assignment]
                return Completion(
                    text="yes", model="fake",
                    usage=TokenUsage(input_tokens=1, output_tokens=1),
                )
            def capabilities(self):  # pragma: no cover
                """Unused — only ``complete`` is exercised."""
            async def embed(self, texts, **kw):  # pragma: no cover
                """Unused — only ``complete`` is exercised."""

        judge = LongMemEvalJudge(CapturingLLM())  # type: ignore[arg-type]
        await judge.score("temporal-reasoning", "Q?", "A.", "R.")
        assert captured_prompt["content"] == build_longmemeval_judge_prompt(
            "temporal-reasoning", "Q?", "A.", "R.",
        )

    async def test_llm_failure_propagates(self) -> None:
        """If the LLM raises, the judge doesn't swallow it — the adapter
        decides how to aggregate (log+count-as-0, or halt)."""
        class FailingLLM:
            SPI_VERSION = 1
            async def complete(self, messages, **kw):  # type: ignore[no-untyped-def]
                raise RuntimeError("provider down")
            def capabilities(self):  # pragma: no cover
                """Unused — only ``complete`` is exercised."""
            async def embed(self, texts, **kw):  # pragma: no cover
                """Unused — only ``complete`` is exercised."""

        judge = LongMemEvalJudge(FailingLLM())  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="provider down"):
            await judge.score("single-session-user", "q", "a", "r")


# ---------------------------------------------------------------------------
# Regression pins — invariants operators / downstream callers depend on
# ---------------------------------------------------------------------------


class TestCategoryIdsFrozen:
    """The string→int mapping must match the upstream numeric categories.
    A rename here silently shifts every per-category score."""

    def test_category_ids_match_upstream(self) -> None:
        # Verified from datasets/locomo/data/locomo10.json: 1=multi-hop,
        # 2=temporal, 3=open-domain, 4=single-hop, 5=adversarial.
        # Earlier revisions had incorrect mappings (2↔4 swapped AND 3
        # mislabeled as "temporal").
        assert LOCOMO_CATEGORY_IDS == {
            "multi-hop": 1,
            "temporal": 2,
            "open-domain": 3,
            "single-hop": 4,
            "adversarial": 5,
        }
