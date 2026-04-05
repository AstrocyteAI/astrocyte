"""Tests for LoCoMo benchmark adapter."""

import json
from pathlib import Path

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.eval.benchmarks.locomo import (
    LoComoBenchmark,
    LoCoMoConversation,
    LoCoMoQuestion,
    LoCoMoSession,
    load_locomo_dataset,
)
from astrocyte.testing.in_memory import InMemoryEngineProvider


def _make_brain() -> tuple[Astrocyte, InMemoryEngineProvider]:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


class TestLoComoBenchmark:
    async def test_run_with_synthetic_conversations(self):
        brain, _ = _make_brain()
        bench = LoComoBenchmark(brain)

        conversations = [
            LoCoMoConversation(
                conversation_id="c1",
                sessions=[
                    LoCoMoSession(
                        session_id="session_1",
                        turns=[
                            {"speaker": "Alice", "text": "I just got a new puppy named Max"},
                            {"speaker": "Bob", "text": "That's great! What breed is Max?"},
                            {"speaker": "Alice", "text": "He's a golden retriever"},
                        ],
                        date_time="2026-01-15",
                    ),
                    LoCoMoSession(
                        session_id="session_2",
                        turns=[
                            {"speaker": "Alice", "text": "Max learned to sit today"},
                            {"speaker": "Bob", "text": "Good boy! How old is he now?"},
                            {"speaker": "Alice", "text": "He's 4 months old"},
                        ],
                        date_time="2026-02-10",
                    ),
                ],
                questions=[
                    LoCoMoQuestion(
                        question="What is the name of Alice's dog?",
                        answer="Max",
                        category="single-hop",
                        evidence_ids=["dia_1"],
                        conversation_id="c1",
                    ),
                    LoCoMoQuestion(
                        question="What breed is Alice's dog?",
                        answer="golden retriever",
                        category="single-hop",
                        evidence_ids=["dia_3"],
                        conversation_id="c1",
                    ),
                    LoCoMoQuestion(
                        question="What trick did Max learn?",
                        answer="sit",
                        category="multi-hop",
                        evidence_ids=["dia_4"],
                        conversation_id="c1",
                    ),
                ],
            ),
        ]

        result = await bench.run(conversations=conversations, bank_id="bench-locomo-test")

        assert result.total_questions == 3
        assert result.overall_accuracy >= 0.0
        assert len(result.per_question) == 3
        assert "single-hop" in result.category_accuracy

    async def test_cleanup(self):
        brain, engine = _make_brain()
        bench = LoComoBenchmark(brain)

        conversations = [
            LoCoMoConversation(
                conversation_id="c1",
                sessions=[
                    LoCoMoSession(session_id="s1", turns=[{"speaker": "A", "text": "Hello world"}]),
                ],
                questions=[
                    LoCoMoQuestion(
                        question="What was said?",
                        answer="Hello",
                        category="single-hop",
                        evidence_ids=[],
                        conversation_id="c1",
                    ),
                ],
            ),
        ]

        await bench.run(conversations=conversations, bank_id="bench-cleanup", clean_after=True)
        assert len(engine._memories.get("bench-cleanup", [])) == 0

    async def test_no_cleanup(self):
        brain, engine = _make_brain()
        bench = LoComoBenchmark(brain)

        conversations = [
            LoCoMoConversation(
                conversation_id="c1",
                sessions=[
                    LoCoMoSession(session_id="s1", turns=[{"speaker": "A", "text": "persistent data"}]),
                ],
                questions=[
                    LoCoMoQuestion(
                        question="test?",
                        answer="persistent",
                        category="single-hop",
                        evidence_ids=[],
                        conversation_id="c1",
                    ),
                ],
            ),
        ]

        await bench.run(conversations=conversations, bank_id="bench-keep", clean_after=False)
        assert len(engine._memories.get("bench-keep", [])) >= 1

    async def test_eval_result_structure(self):
        brain, _ = _make_brain()
        bench = LoComoBenchmark(brain)

        conversations = [
            LoCoMoConversation(
                conversation_id="c1",
                sessions=[
                    LoCoMoSession(session_id="s1", turns=[{"speaker": "A", "text": "Python is great"}]),
                ],
                questions=[
                    LoCoMoQuestion(
                        question="What language?",
                        answer="Python",
                        category="single-hop",
                        evidence_ids=[],
                        conversation_id="c1",
                    ),
                ],
            ),
        ]

        result = await bench.run(conversations=conversations, bank_id="bench-struct")

        er = result.eval_result
        assert er.suite == "locomo"
        assert er.metrics.total_duration_seconds > 0
        assert er.config_snapshot["benchmark"] == "locomo"

    async def test_max_questions(self):
        brain, _ = _make_brain()
        bench = LoComoBenchmark(brain)

        questions = [
            LoCoMoQuestion(
                question=f"Q{i}?", answer=f"A{i}", category="single-hop", evidence_ids=[], conversation_id="c1"
            )
            for i in range(10)
        ]
        conversations = [
            LoCoMoConversation(
                conversation_id="c1",
                sessions=[LoCoMoSession(session_id="s1", turns=[{"speaker": "A", "text": "context"}])],
                questions=questions,
            ),
        ]

        result = await bench.run(conversations=conversations, bank_id="bench-limit", max_questions=3)
        assert result.total_questions == 3

    async def test_multiple_categories(self):
        brain, _ = _make_brain()
        bench = LoComoBenchmark(brain)

        conversations = [
            LoCoMoConversation(
                conversation_id="c1",
                sessions=[
                    LoCoMoSession(
                        session_id="s1",
                        turns=[
                            {"speaker": "A", "text": "I like pizza. Last Monday I ordered from Dominos."},
                        ],
                    ),
                ],
                questions=[
                    LoCoMoQuestion(
                        question="What food?",
                        answer="pizza",
                        category="single-hop",
                        evidence_ids=[],
                        conversation_id="c1",
                    ),
                    LoCoMoQuestion(
                        question="When and where did they order?",
                        answer="Monday Dominos",
                        category="multi-hop",
                        evidence_ids=[],
                        conversation_id="c1",
                    ),
                    LoCoMoQuestion(
                        question="What happened recently?",
                        answer="ordered pizza",
                        category="temporal",
                        evidence_ids=[],
                        conversation_id="c1",
                    ),
                ],
            ),
        ]

        result = await bench.run(conversations=conversations, bank_id="bench-cats")
        assert "single-hop" in result.category_accuracy
        assert "multi-hop" in result.category_accuracy
        assert "temporal" in result.category_accuracy


class TestLoCoMoDataLoading:
    def test_load_from_json_array(self, tmp_path: Path):
        data = [
            {
                "id": "convo-1",
                "session_1": [
                    {"speaker": "Alice", "text": "Hello Bob"},
                    {"speaker": "Bob", "text": "Hi Alice"},
                ],
                "session_1_date_time": "2026-01-15",
                "qa": [
                    {
                        "question": "Who greeted whom?",
                        "answer": "Alice greeted Bob",
                        "category": "single-hop",
                        "evidence": ["dia_1"],
                    },
                ],
            },
        ]

        json_file = tmp_path / "locomo10.json"
        json_file.write_text(json.dumps(data))

        conversations = load_locomo_dataset(tmp_path)
        assert len(conversations) == 1
        assert conversations[0].conversation_id == "convo-1"
        assert len(conversations[0].sessions) == 1
        assert len(conversations[0].questions) == 1
        assert conversations[0].questions[0].category == "single-hop"

    def test_load_from_direct_file(self, tmp_path: Path):
        data = [
            {"id": "c1", "session_1": [{"speaker": "A", "text": "test"}], "qa": [{"question": "Q?", "answer": "A"}]}
        ]
        json_file = tmp_path / "data.json"
        json_file.write_text(json.dumps(data))

        conversations = load_locomo_dataset(json_file)
        assert len(conversations) == 1

    def test_load_with_max_conversations(self, tmp_path: Path):
        data = [
            {
                "id": f"c{i}",
                "session_1": [{"speaker": "A", "text": f"text {i}"}],
                "qa": [{"question": f"Q{i}?", "answer": f"A{i}"}],
            }
            for i in range(10)
        ]
        (tmp_path / "data.json").write_text(json.dumps(data))

        conversations = load_locomo_dataset(tmp_path, max_conversations=3)
        assert len(conversations) == 3

    def test_load_no_files_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_locomo_dataset(tmp_path / "nonexistent")

    def test_sessions_parsed_with_datetime(self, tmp_path: Path):
        data = [
            {
                "id": "c1",
                "session_1": [{"speaker": "A", "text": "hello"}],
                "session_1_date_time": "January 15, 2026",
                "session_2": [{"speaker": "B", "text": "world"}],
                "session_2_date_time": "February 10, 2026",
                "qa": [],
            },
        ]
        (tmp_path / "locomo10.json").write_text(json.dumps(data))

        conversations = load_locomo_dataset(tmp_path)
        assert len(conversations[0].sessions) == 2
        assert conversations[0].sessions[0].date_time == "January 15, 2026"
