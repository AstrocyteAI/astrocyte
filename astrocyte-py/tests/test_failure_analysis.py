"""Benchmark failure analysis tests."""

from astrocyte.eval.failure_analysis import analyze_failures, stable_question_slice


def test_analyze_failures_groups_actionable_buckets() -> None:
    result = {
        "per_question": [
            {
                "question": "When did Alice go hiking?",
                "expected_answer": "last week",
                "category": "temporal",
                "correct": False,
                "_evidence_id_hit": True,
                "_reciprocal_rank": 0.2,
                "_relevant_found": 1,
                "recall_top_hits": [{"text_preview": "Bob went hiking."}],
            },
            {
                "question": "What would Caroline likely pursue?",
                "expected_answer": "counseling",
                "category": "open-domain",
                "correct": False,
                "_evidence_id_hit": False,
                "_reciprocal_rank": 0.0,
                "_relevant_found": 0,
            },
        ],
    }

    analysis = analyze_failures(result)

    assert analysis["total_failed"] == 2
    assert analysis["buckets"]["evidence_present_but_low_rank"]["count"] == 1
    assert analysis["buckets"]["temporal_normalization_miss"]["count"] == 1
    assert analysis["buckets"]["open_domain_inference_miss"]["count"] == 1
    assert analysis["recommendations"][0].startswith("Improve reranking")


def test_stable_question_slice_is_deterministic() -> None:
    result = {
        "per_question": [
            {"question": "q1"},
            {"question": "q2"},
            {"question": "q3"},
        ],
    }

    first = stable_question_slice(result, size=2, seed="x")
    second = stable_question_slice(result, size=2, seed="x")

    assert first == second
    assert len(first) == 2
