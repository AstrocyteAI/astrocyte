"""Tests for LoCoMo benchmark adapter."""

import asyncio
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
from astrocyte.pipeline.tasks import COMPILE_PERSONA_PAGE, MemoryTask
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
        brain, engine = _make_brain()
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

        result = await bench.run(conversations=conversations, bank_id="bench-locomo-test", clean_after=False)

        assert result.total_questions == 3
        assert result.overall_accuracy >= 0.0
        assert len(result.per_question) == 3
        assert "single-hop" in result.category_accuracy
        first = result.per_question[0]
        assert first["evidence_ids"] == ["dia_1"]
        assert "recall_top_hits" in first
        assert "reflect_sources" in first
        assert "_relevant_found" in first
        assert "_evidence_id_hit" in first
        stored = engine._memories["bench-locomo-test"]
        raw = next(mem for mem in stored if mem.metadata and mem.metadata.get("source") == "locomo")
        assert raw.metadata["extraction_profile"] == "locomo_conversation"
        assert raw.metadata["locomo_speakers"] == "Alice,Bob"
        assert raw.metadata["locomo_turn_count"] == 3
        persona = next(mem for mem in stored if mem.metadata and mem.metadata.get("source") == "locomo_persona_compile")
        assert persona.metadata["person"] == "Alice,Bob"
        assert "_wiki_source_ids" in persona.metadata

    async def test_persona_compile_is_enqueued_when_task_queue_is_configured(self):
        class RecordingQueue:
            def __init__(self) -> None:
                self.tasks: list[MemoryTask] = []

            async def enqueue(self, task: MemoryTask) -> str:
                self.tasks.append(task)
                return task.id

        brain, engine = _make_brain()
        queue = RecordingQueue()
        setattr(brain, "_benchmark_task_queue", queue)
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
                        ],
                    ),
                ],
                questions=[
                    LoCoMoQuestion(
                        question="What is the name of Alice's dog?",
                        answer="Max",
                        category="single-hop",
                        evidence_ids=[],
                        conversation_id="c1",
                    ),
                ],
            ),
        ]

        await bench.run(conversations=conversations, bank_id="bench-locomo-queue", clean_after=False)

        assert {task.task_type for task in queue.tasks} == {COMPILE_PERSONA_PAGE}
        assert all(task.payload["index_vector"] is True for task in queue.tasks)
        stored = engine._memories["bench-locomo-queue"]
        assert not any(mem.metadata and mem.metadata.get("source") == "locomo_persona_compile" for mem in stored)

    async def test_retain_phase_uses_configured_concurrency(self):
        class SlowEngine(InMemoryEngineProvider):
            def __init__(self) -> None:
                super().__init__()
                self.active = 0
                self.max_active = 0

            async def retain(self, request):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                try:
                    await asyncio.sleep(0.01)
                    return await super().retain(request)
                finally:
                    self.active -= 1

        config = AstrocyteConfig()
        config.provider = "test"
        config.barriers.pii.mode = "disabled"
        brain = Astrocyte(config)
        engine = SlowEngine()
        brain.set_engine_provider(engine)
        bench = LoComoBenchmark(brain)
        conversations = [
            LoCoMoConversation(
                conversation_id="c1",
                sessions=[
                    LoCoMoSession(session_id=f"s{i}", turns=[{"speaker": "A", "text": f"memory {i}"}])
                    for i in range(6)
                ],
                questions=[
                    LoCoMoQuestion(
                        question="What was said?",
                        answer="memory",
                        category="single-hop",
                        evidence_ids=[],
                        conversation_id="c1",
                    ),
                ],
            ),
        ]

        await bench.run(
            conversations=conversations,
            bank_id="bench-locomo-concurrent",
            clean_after=True,
            retain_concurrency=3,
        )

        assert engine.max_active > 1

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

    async def test_max_questions_per_conversation_takes_first_n_per_convo(self):
        """Per-conversation cap gives uniform coverage across conversations,
        unlike ``max_questions`` which head-slices and over-weights early
        conversations. 2 questions × 3 conversations = 6 questions total
        with one from each conversation represented."""
        brain, _ = _make_brain()
        bench = LoComoBenchmark(brain)

        def _convo(cid: str, n: int) -> LoCoMoConversation:
            return LoCoMoConversation(
                conversation_id=cid,
                sessions=[LoCoMoSession(session_id=f"s-{cid}", turns=[{"speaker": "A", "text": "ctx"}])],
                questions=[
                    LoCoMoQuestion(
                        question=f"{cid}-Q{i}", answer=f"A{i}",
                        category="single-hop", evidence_ids=[], conversation_id=cid,
                    )
                    for i in range(n)
                ],
            )
        conversations = [_convo("c-A", 5), _convo("c-B", 5), _convo("c-C", 5)]

        result = await bench.run(
            conversations=conversations, bank_id="bench-fair",
            max_questions_per_conversation=2,
        )

        # Total = 2 * 3 = 6, with each conversation contributing 2 questions.
        assert result.total_questions == 6
        # Question text is "{cid}-Q{i}" so we can recover which conversations
        # were sampled. All three must appear (no head-slicing into c-A only).
        convos_seen = {q["question"].split("-Q")[0] for q in result.per_question}
        assert convos_seen == {"c-A", "c-B", "c-C"}, (
            f"Fair sampling must include every conversation, got convos="
            f"{convos_seen} from questions={[q['question'] for q in result.per_question]}"
        )

    async def test_max_questions_per_conversation_stratifies_across_categories(self):
        """Per-conversation sampling MUST stratify across categories so that
        rare categories (e.g. adversarial, which historically sits at the
        end of LoCoMo's question list) aren't systematically excluded by
        a head-slice.

        Regression: 2026-05-02 fair-bench produced 0 adversarial questions
        out of 200 because the prior head-slice grabbed [:20] of each
        conversation's questions, missing categories at the tail.
        """
        brain, _ = _make_brain()
        bench = LoComoBenchmark(brain)

        # Conversation with 5 categories, 10 questions per category, 50 total.
        # Categories appear in distinct ranges so a naive head-slice would
        # grab only the first one.
        categories = ["multi-hop", "open-domain", "single-hop", "temporal", "adversarial"]
        questions = []
        for cat_idx, cat in enumerate(categories):
            for j in range(10):
                questions.append(
                    LoCoMoQuestion(
                        question=f"{cat}-Q{j}", answer=f"A{j}",
                        category=cat, evidence_ids=[], conversation_id="c1",
                    )
                )
        # Confirm test setup: head-slice [:5] would grab only multi-hop.
        assert {q.category for q in questions[:5]} == {"multi-hop"}

        conversations = [
            LoCoMoConversation(
                conversation_id="c1",
                sessions=[LoCoMoSession(session_id="s", turns=[{"speaker": "A", "text": "ctx"}])],
                questions=questions,
            ),
        ]

        # Take 5 per conversation → with 5 categories present, take 1 each.
        # The fix must include EVERY category, not just multi-hop.
        result = await bench.run(
            conversations=conversations, bank_id="bench-strat",
            max_questions_per_conversation=5,
        )

        cats_seen = {q["category"] for q in result.per_question}
        assert cats_seen == set(categories), (
            f"Stratified sampling must include every category present in the "
            f"conversation. Got {cats_seen}, missing {set(categories) - cats_seen}. "
            f"This is the bug that produced 0 adversarial questions on 2026-05-02."
        )

    async def test_max_questions_per_conversation_combines_with_total_cap(self):
        """When both flags are set, per-conversation runs first, then the
        total cap is applied as a head-slice. 3 per-convo × 4 convos = 12,
        capped at 5 → 5 total."""
        brain, _ = _make_brain()
        bench = LoComoBenchmark(brain)

        conversations = [
            LoCoMoConversation(
                conversation_id=f"c{i}",
                sessions=[LoCoMoSession(session_id=f"s{i}", turns=[{"speaker": "A", "text": "ctx"}])],
                questions=[
                    LoCoMoQuestion(
                        question=f"c{i}-Q{j}", answer=f"A{j}",
                        category="single-hop", evidence_ids=[], conversation_id=f"c{i}",
                    )
                    for j in range(5)
                ],
            )
            for i in range(4)
        ]

        result = await bench.run(
            conversations=conversations, bank_id="bench-both",
            max_questions_per_conversation=3,
            max_questions=5,
        )

        assert result.total_questions == 5

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
