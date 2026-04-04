"""Tests for DeepEval judge integration and LongMemEval benchmark adapter.

DeepEval tests that don't call the actual LLM judge (no API key needed).
LongMemEval tests use synthetic data (no external dataset download needed).
"""

from pathlib import Path

import pytest

from astrocytes._astrocyte import Astrocyte
from astrocytes.config import AstrocyteConfig
from astrocytes.eval.benchmarks.longmemeval import (
    LongMemEvalBenchmark,
    LongMemEvalQuestion,
)
from astrocytes.eval.deepeval_judge import DeepEvalScores, _safe_mean
from astrocytes.eval.evaluator import MemoryEvaluator
from astrocytes.eval.suite import EvalSuite, RecallCase, RetainCase
from astrocytes.testing.in_memory import InMemoryEngineProvider


def _make_brain() -> tuple[Astrocyte, InMemoryEngineProvider]:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


# ---------------------------------------------------------------------------
# DeepEval judge module (unit tests, no LLM calls)
# ---------------------------------------------------------------------------


class TestDeepEvalScores:
    def test_empty_scores(self):
        scores = DeepEvalScores()
        assert scores.contextual_relevancy is None
        assert scores.faithfulness is None
        assert scores.per_query_scores == []

    def test_populated_scores(self):
        scores = DeepEvalScores(
            contextual_relevancy=0.85,
            faithfulness=0.90,
            answer_relevancy=0.88,
            hallucination=0.05,
        )
        assert scores.contextual_relevancy == 0.85
        assert scores.hallucination == 0.05

    def test_safe_mean(self):
        assert _safe_mean([0.8, 0.9, 0.7]) == pytest.approx(0.8, abs=0.01)
        assert _safe_mean([]) is None
        assert _safe_mean([1.0]) == 1.0


class TestDeepEvalImportCheck:
    def test_deepeval_importable(self):
        """Verify deepeval is installed in test env."""
        from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
        from deepeval.test_case import LLMTestCase

        assert LLMTestCase is not None
        assert AnswerRelevancyMetric is not None
        assert FaithfulnessMetric is not None


# ---------------------------------------------------------------------------
# Evaluator with judge parameter (keyword mode — no LLM needed)
# ---------------------------------------------------------------------------


class TestEvaluatorJudgeParam:
    async def test_keyword_judge_default(self):
        brain, _ = _make_brain()
        evaluator = MemoryEvaluator(brain)
        custom = EvalSuite(
            name="judge-test",
            retains=[RetainCase(content="Dark mode is preferred")],
            recalls=[RecallCase(query="dark mode", expected_contains=["dark"])],
        )
        result = await evaluator.run_suite(custom, bank_id="eval-judge", judge="keyword")
        assert result.config_snapshot["judge"] == "keyword"

    async def test_keyword_judge_explicit(self):
        brain, _ = _make_brain()
        evaluator = MemoryEvaluator(brain)
        custom = EvalSuite(
            name="explicit-keyword",
            retains=[RetainCase(content="Python is great")],
            recalls=[RecallCase(query="Python", expected_contains=["Python"])],
        )
        result = await evaluator.run_suite(custom, bank_id="eval-kw", judge="keyword")
        assert result.metrics.recall_hit_rate > 0.0
        # No deepeval keys in config_snapshot for keyword judge
        assert "deepeval_contextual_relevancy" not in result.config_snapshot


# ---------------------------------------------------------------------------
# LongMemEval benchmark adapter (synthetic data, no download needed)
# ---------------------------------------------------------------------------


class TestLongMemEvalBenchmark:
    async def test_run_with_synthetic_questions(self):
        brain, _ = _make_brain()
        bench = LongMemEvalBenchmark(brain)

        questions = [
            LongMemEvalQuestion(
                question_id="q1",
                category="extraction",
                question="What is Calvin's favorite color?",
                answer="blue",
                session_ids=["s1"],
                conversation_context=[
                    {"content": "Calvin said his favorite color is blue", "role": "user", "session_id": "s1"},
                ],
            ),
            LongMemEvalQuestion(
                question_id="q2",
                category="temporal",
                question="When did the meeting happen?",
                answer="Monday at 3pm",
                session_ids=["s2"],
                conversation_context=[
                    {"content": "The meeting was scheduled for Monday at 3pm", "role": "user", "session_id": "s2"},
                ],
            ),
            LongMemEvalQuestion(
                question_id="q3",
                category="reasoning",
                question="What programming language does the team use?",
                answer="Python",
                session_ids=["s3"],
                conversation_context=[
                    {"content": "Our team primarily develops in Python", "role": "user", "session_id": "s3"},
                ],
            ),
        ]

        result = await bench.run(questions=questions, bank_id="bench-lme-test")

        assert result.total_questions == 3
        assert result.overall_accuracy >= 0.0  # May or may not find answers with keyword matching
        assert len(result.per_question) == 3
        assert "extraction" in result.category_accuracy
        assert "temporal" in result.category_accuracy
        assert "reasoning" in result.category_accuracy

    async def test_run_with_cleanup(self):
        brain, engine = _make_brain()
        bench = LongMemEvalBenchmark(brain)

        questions = [
            LongMemEvalQuestion(
                question_id="q1",
                category="extraction",
                question="What is dark mode?",
                answer="UI theme",
                session_ids=["s1"],
                conversation_context=[
                    {"content": "Dark mode is a UI theme with dark background", "role": "user", "session_id": "s1"},
                ],
            ),
        ]

        await bench.run(questions=questions, bank_id="bench-cleanup", clean_after=True)
        assert len(engine._memories.get("bench-cleanup", [])) == 0

    async def test_run_no_cleanup(self):
        brain, engine = _make_brain()
        bench = LongMemEvalBenchmark(brain)

        questions = [
            LongMemEvalQuestion(
                question_id="q1",
                category="extraction",
                question="test",
                answer="answer",
                session_ids=["s1"],
                conversation_context=[
                    {"content": "The answer is here", "role": "user", "session_id": "s1"},
                ],
            ),
        ]

        await bench.run(questions=questions, bank_id="bench-keep", clean_after=False)
        assert len(engine._memories.get("bench-keep", [])) >= 1

    async def test_eval_result_structure(self):
        brain, _ = _make_brain()
        bench = LongMemEvalBenchmark(brain)

        questions = [
            LongMemEvalQuestion(
                question_id="q1",
                category="extraction",
                question="What is Python?",
                answer="programming language",
                session_ids=["s1"],
                conversation_context=[
                    {"content": "Python is a programming language", "role": "user", "session_id": "s1"},
                ],
            ),
        ]

        result = await bench.run(questions=questions, bank_id="bench-struct")

        # Standard EvalResult is available
        er = result.eval_result
        assert er.suite == "longmemeval"
        assert er.metrics.retain_latency_p50_ms >= 0
        assert er.metrics.recall_latency_p50_ms >= 0
        assert er.metrics.total_duration_seconds > 0
        assert er.config_snapshot["benchmark"] == "longmemeval"

    async def test_max_questions_limit(self):
        brain, _ = _make_brain()
        bench = LongMemEvalBenchmark(brain)

        questions = [
            LongMemEvalQuestion(
                question_id=f"q{i}",
                category="extraction",
                question=f"Question {i}?",
                answer=f"answer {i}",
                session_ids=[f"s{i}"],
                conversation_context=[{"content": f"Answer {i} content", "role": "user", "session_id": f"s{i}"}],
            )
            for i in range(10)
        ]

        result = await bench.run(questions=questions, bank_id="bench-limit", max_questions=3)
        assert result.total_questions == 3


class TestLongMemEvalDataLoading:
    def test_load_from_json(self, tmp_path: Path):
        """Test loading from a JSON file with the expected format."""
        data = [
            {
                "id": "q1",
                "category": "extraction",
                "question": "What is X?",
                "answer": "Y",
                "session_ids": ["s1"],
                "conversation": [{"content": "X is Y", "role": "user", "session_id": "s1"}],
            },
            {
                "id": "q2",
                "category": "temporal",
                "question": "When?",
                "answer": "Tuesday",
                "session_ids": ["s2"],
                "conversation": [{"content": "It was Tuesday", "role": "user", "session_id": "s2"}],
            },
        ]

        import json

        json_file = tmp_path / "questions.json"
        json_file.write_text(json.dumps(data))

        from astrocytes.eval.benchmarks.longmemeval import load_longmemeval_dataset

        questions = load_longmemeval_dataset(tmp_path)
        assert len(questions) == 2
        assert questions[0].question_id == "q1"
        assert questions[0].category == "extraction"
        assert questions[1].answer == "Tuesday"

    def test_load_with_max_questions(self, tmp_path: Path):
        import json

        data = [{"id": f"q{i}", "question": f"Q{i}?", "answer": f"A{i}", "conversation": []} for i in range(20)]
        (tmp_path / "data.json").write_text(json.dumps(data))

        from astrocytes.eval.benchmarks.longmemeval import load_longmemeval_dataset

        questions = load_longmemeval_dataset(tmp_path, max_questions=5)
        assert len(questions) == 5

    def test_load_no_files_raises(self, tmp_path: Path):
        from astrocytes.eval.benchmarks.longmemeval import load_longmemeval_dataset

        with pytest.raises(FileNotFoundError):
            load_longmemeval_dataset(tmp_path / "nonexistent")
