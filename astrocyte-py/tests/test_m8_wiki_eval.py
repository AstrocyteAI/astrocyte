"""M8 W7: Wiki eval harness — unit and integration tests.

Tests cover:
- WikiEvalCase / WikiABResult dataclasses
- _is_correct() accuracy scorer
- WikiCompileEvalHarness.run_ab():
    - Returns WikiABResult with correct field types
    - wiki_tier_used flag set when wiki tier fires
    - Correct per-category breakdown
    - Empty cases edge case
- assert_wiki_regression_gate():
    - Passes when wiki >= baseline
    - Raises with informative message when wiki < baseline
    - Skips categories not in the result
- Built-in synthetic fixture (make_synthetic_cases):
    - Wiki accuracy >= baseline accuracy (regression gate)
    - Wiki tier fires on at least some cases

The harness uses MockLLMProvider bag-of-words embeddings, which give real
semantic signal (tokens shared between query and memory text raise cosine
similarity).  The compile step's LLM synthesis uses MockLLMProvider's
default_response, so tests configure that response to contain the expected
keywords so the compiled wiki page can be scored as correct.
"""

from __future__ import annotations

import pytest

from astrocyte.eval.wiki_eval import (
    WikiABResult,
    WikiCompileEvalHarness,
    WikiEvalCase,
    _is_correct,
    assert_wiki_regression_gate,
    make_synthetic_cases,
)
from astrocyte.testing.in_memory import MockLLMProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _harness(response: str = "Mock LLM response", threshold: float = 0.5) -> WikiCompileEvalHarness:
    """Create a harness backed by MockLLMProvider with a given synthesis response."""
    llm = MockLLMProvider(default_response=response)
    return WikiCompileEvalHarness(llm, wiki_confidence_threshold=threshold)


def _case(
    case_id: str = "test-case",
    category: str = "knowledge-update",
    retains: list[str] | None = None,
    scope: str = "test-scope",
    query: str = "What is the answer?",
    expected_keywords: list[str] | None = None,
) -> WikiEvalCase:
    return WikiEvalCase(
        case_id=case_id,
        category=category,
        retains=retains or ["The answer is forty two."],
        scope=scope,
        query=query,
        expected_keywords=expected_keywords or ["forty two"],
    )


def _ab_result(baseline: float, wiki: float, breakdown: dict | None = None) -> WikiABResult:
    n = 10
    return WikiABResult(
        baseline_accuracy=baseline,
        wiki_accuracy=wiki,
        lift=wiki - baseline,
        total_cases=n,
        baseline_correct=int(baseline * n),
        wiki_correct=int(wiki * n),
        category_breakdown=breakdown or {},
    )


# ---------------------------------------------------------------------------
# _is_correct
# ---------------------------------------------------------------------------


class TestIsCorrect:
    def test_keyword_found_in_hit(self):
        assert _is_correct(["Alice works at Meta"], ["Meta"])

    def test_keyword_case_insensitive(self):
        assert _is_correct(["alice works at meta"], ["Meta"])

    def test_keyword_in_any_hit(self):
        assert _is_correct(["irrelevant", "Alice works at Meta"], ["Meta"])

    def test_no_keyword_found(self):
        assert not _is_correct(["Alice works at Google"], ["Meta"])

    def test_empty_hits(self):
        assert not _is_correct([], ["Meta"])

    def test_multiple_keywords_any_matches(self):
        assert _is_correct(["Alice is at Google"], ["Meta", "Google"])

    def test_empty_keywords(self):
        assert not _is_correct(["some text"], [])


# ---------------------------------------------------------------------------
# WikiABResult properties
# ---------------------------------------------------------------------------


class TestWikiABResultProperties:
    def test_meets_gate_when_wiki_equal(self):
        r = _ab_result(0.5, 0.5)
        assert r.meets_gate is True

    def test_meets_gate_when_wiki_greater(self):
        r = _ab_result(0.4, 0.7)
        assert r.meets_gate is True

    def test_gate_fails_when_wiki_lower(self):
        r = _ab_result(0.6, 0.4)
        assert r.meets_gate is False

    def test_meets_lift_target(self):
        r = _ab_result(0.4, 0.5)
        assert r.meets_lift_target is False
        r2 = _ab_result(0.3, 0.5)
        assert r2.meets_lift_target is True  # 0.2 >= 0.1

    def test_lift_computed(self):
        r = _ab_result(0.3, 0.7)
        assert abs(r.lift - 0.4) < 1e-9


# ---------------------------------------------------------------------------
# assert_wiki_regression_gate
# ---------------------------------------------------------------------------


class TestRegressionGate:
    def test_passes_when_wiki_better(self):
        r = _ab_result(
            0.4,
            0.7,
            breakdown={
                "knowledge-update": {"baseline_accuracy": 0.4, "wiki_accuracy": 0.7, "lift": 0.3, "total": 5},
                "multi-session": {"baseline_accuracy": 0.5, "wiki_accuracy": 0.6, "lift": 0.1, "total": 5},
            },
        )
        assert_wiki_regression_gate(r)  # Should not raise

    def test_passes_when_wiki_equal(self):
        r = _ab_result(
            0.5,
            0.5,
            breakdown={
                "knowledge-update": {"baseline_accuracy": 0.5, "wiki_accuracy": 0.5, "lift": 0.0, "total": 5},
            },
        )
        assert_wiki_regression_gate(r)  # Equal is OK — not a regression

    def test_fails_when_wiki_worse(self):
        r = _ab_result(
            0.7,
            0.4,
            breakdown={
                "knowledge-update": {"baseline_accuracy": 0.7, "wiki_accuracy": 0.4, "lift": -0.3, "total": 5},
                "multi-session": {"baseline_accuracy": 0.6, "wiki_accuracy": 0.6, "lift": 0.0, "total": 5},
            },
        )
        with pytest.raises(AssertionError, match="knowledge-update"):
            assert_wiki_regression_gate(r)

    def test_error_message_names_failing_category(self):
        r = _ab_result(
            0.8,
            0.3,
            breakdown={
                "knowledge-update": {"baseline_accuracy": 0.8, "wiki_accuracy": 0.3, "lift": -0.5, "total": 5},
            },
        )
        with pytest.raises(AssertionError) as exc_info:
            assert_wiki_regression_gate(r)
        msg = str(exc_info.value)
        assert "knowledge-update" in msg
        assert "wiki=" in msg
        assert "baseline=" in msg

    def test_skips_category_not_in_result(self):
        r = _ab_result(
            0.5,
            0.5,
            breakdown={
                "extraction": {"baseline_accuracy": 0.9, "wiki_accuracy": 0.1, "lift": -0.8, "total": 5},
            },
        )
        # Gate only monitors knowledge-update and multi-session by default;
        # a regression in "extraction" does not trigger it.
        assert_wiki_regression_gate(r)  # Should not raise

    def test_custom_categories(self):
        r = _ab_result(
            0.5,
            0.3,
            breakdown={
                "extraction": {"baseline_accuracy": 0.5, "wiki_accuracy": 0.3, "lift": -0.2, "total": 5},
            },
        )
        with pytest.raises(AssertionError, match="extraction"):
            assert_wiki_regression_gate(r, categories=["extraction"])


# ---------------------------------------------------------------------------
# WikiCompileEvalHarness — structural tests
# ---------------------------------------------------------------------------


class TestHarnessStructure:
    @pytest.mark.asyncio
    async def test_empty_cases_returns_zero_result(self):
        h = _harness()
        result = await h.run_ab([])
        assert result.total_cases == 0
        assert result.baseline_accuracy == 0.0
        assert result.wiki_accuracy == 0.0
        assert result.lift == 0.0

    @pytest.mark.asyncio
    async def test_result_has_all_fields(self):
        h = _harness(response="forty two is the answer")
        case = _case(
            retains=["The answer is forty two."],
            query="What is the answer?",
            expected_keywords=["forty two"],
        )
        result = await h.run_ab([case])
        assert isinstance(result.total_cases, int)
        assert isinstance(result.baseline_accuracy, float)
        assert isinstance(result.wiki_accuracy, float)
        assert isinstance(result.lift, float)
        assert isinstance(result.per_case, list)
        assert len(result.per_case) == 1
        assert isinstance(result.category_breakdown, dict)

    @pytest.mark.asyncio
    async def test_per_case_result_fields(self):
        h = _harness(response="forty two is the answer")
        case = _case(
            retains=["The answer is forty two."],
            query="What is the answer?",
            expected_keywords=["forty two"],
        )
        result = await h.run_ab([case])
        cr = result.per_case[0]
        assert cr.case_id == case.case_id
        assert cr.category == case.category
        assert isinstance(cr.baseline_correct, bool)
        assert isinstance(cr.wiki_correct, bool)
        assert isinstance(cr.wiki_tier_used, bool)
        assert isinstance(cr.baseline_top_text, str)
        assert isinstance(cr.wiki_top_text, str)

    @pytest.mark.asyncio
    async def test_category_breakdown_populated(self):
        h = _harness(response="the answer is forty two")
        cases = [
            _case("c1", "knowledge-update", ["fact one"], "scope-a", "query one", ["fact"]),
            _case("c2", "multi-session", ["fact two"], "scope-b", "query two", ["fact"]),
        ]
        result = await h.run_ab(cases)
        assert "knowledge-update" in result.category_breakdown
        assert "multi-session" in result.category_breakdown
        ku = result.category_breakdown["knowledge-update"]
        assert "baseline_accuracy" in ku
        assert "wiki_accuracy" in ku
        assert "lift" in ku
        assert ku["total"] == 1

    @pytest.mark.asyncio
    async def test_multiple_cases_counted(self):
        h = _harness(response="the answer is here")
        cases = [_case(f"c{i}", retains=[f"memory {i}"], scope=f"scope-{i}") for i in range(4)]
        result = await h.run_ab(cases)
        assert result.total_cases == 4


# ---------------------------------------------------------------------------
# WikiCompileEvalHarness — regression gate end-to-end
# ---------------------------------------------------------------------------


class TestHarnessRegressionGate:
    @pytest.mark.asyncio
    async def test_gate_passes_on_simple_case(self):
        """Wiki tier must not hurt accuracy on a trivial single-fact case."""
        # The synthesis response contains the expected keyword → wiki correct.
        # Raw memory also contains the keyword → baseline may also be correct.
        # Either way, wiki_accuracy >= baseline_accuracy (gate passes).
        h = _harness(response="Alice works at Meta as engineer")
        case = WikiEvalCase(
            case_id="simple",
            category="knowledge-update",
            retains=["Alice recently joined Meta."],
            scope="alice",
            query="Where does Alice work?",
            expected_keywords=["Meta"],
        )
        result = await h.run_ab([case])
        assert result.meets_gate, (
            f"Gate failed: baseline={result.baseline_accuracy:.1%} wiki={result.wiki_accuracy:.1%}"
        )

    @pytest.mark.asyncio
    async def test_assert_gate_passes(self):
        """assert_wiki_regression_gate should not raise when gate passes."""
        h = _harness(response="Alice works at Meta as engineer")
        case = WikiEvalCase(
            case_id="simple",
            category="knowledge-update",
            retains=["Alice recently joined Meta."],
            scope="alice",
            query="Where does Alice work?",
            expected_keywords=["Meta"],
        )
        result = await h.run_ab([case])
        assert_wiki_regression_gate(result)  # Must not raise


# ---------------------------------------------------------------------------
# make_synthetic_cases
# ---------------------------------------------------------------------------


class TestSyntheticFixture:
    def test_returns_nonempty_list(self):
        cases = make_synthetic_cases()
        assert len(cases) >= 4

    def test_covers_both_monitored_categories(self):
        cases = make_synthetic_cases()
        cats = {c.category for c in cases}
        assert "knowledge-update" in cats
        assert "multi-session" in cats

    def test_cases_have_required_fields(self):
        for case in make_synthetic_cases():
            assert case.case_id
            assert case.category
            assert case.retains
            assert case.scope
            assert case.query
            assert case.expected_keywords

    @pytest.mark.asyncio
    async def test_synthetic_cases_regression_gate(self):
        """Wiki tier must not regress on the built-in synthetic fixture.

        Uses a synthesis response that contains keywords from both monitored
        categories so that the compiled wiki pages can be scored as correct.
        The gate condition (wiki >= baseline) must hold even if the absolute
        accuracy numbers are low.
        """
        # The synthesis response is shared across all compile calls.
        # It should contain keywords from enough cases that the wiki tier
        # is at least as accurate as baseline.
        synthesis = (
            "Meta engineer recently joined. "
            "Project resumed Q2 launch. "
            "forecasting anomaly detection expertise. "
            "database query analytics incident. "
            "Austin Texas headquarters."
        )
        llm = MockLLMProvider(default_response=synthesis)
        h = WikiCompileEvalHarness(llm, wiki_confidence_threshold=0.5, top_k=10)
        cases = make_synthetic_cases()

        result = await h.run_ab(cases, bank_id="eval-synthetic")

        assert_wiki_regression_gate(result)

    @pytest.mark.asyncio
    async def test_wiki_tier_fires_on_at_least_one_case(self):
        """The wiki tier engages when threshold=0.0 (fires on any wiki hit).

        With BoW embeddings the cosine similarity between a query and a wiki
        page may be low (few shared tokens).  Setting threshold=0.0 ensures
        any wiki hit triggers the tier, letting us verify the plumbing works
        end-to-end without depending on semantic similarity magnitude.
        """
        synthesis = "Meta engineer recently joined. forecasting anomaly. Austin Texas."
        llm = MockLLMProvider(default_response=synthesis)
        # threshold=0.0 → wiki tier fires for any wiki hit (score >= 0.0)
        h = WikiCompileEvalHarness(llm, wiki_confidence_threshold=0.0, top_k=10)
        cases = make_synthetic_cases()

        result = await h.run_ab(cases, bank_id="eval-wiki-fires")

        wiki_fired = sum(1 for cr in result.per_case if cr.wiki_tier_used)
        assert wiki_fired >= 1, (
            f"Expected wiki tier to fire on at least 1 case, fired on {wiki_fired}. "
            "Check that CompileEngine stored wiki pages into the VectorStore."
        )
