"""LongMemEval loader tests — Session 1 Item 1 fix.

Pins the session-level retention contract after fixing the bug where the
loader emitted one `conversation_context` entry per turn and the retain
loop deduped by session_id, so we stored the first turn of each session
and discarded the rest. Post-fix each session concatenates into a single
entry that carries all turns with role markers.

Root cause writeup: see `docs/_design/platform-positioning.md` §LongMemEval
root causes; conversation thread captured in this test's docstring.
"""

from __future__ import annotations

import json
from pathlib import Path

from astrocyte.eval.benchmarks.longmemeval import (
    _parse_longmemeval_date,
    load_longmemeval_dataset,
)


def _write_fixture(tmp_path: Path, data: list[dict]) -> Path:
    path = tmp_path / "longmemeval.json"
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# The session-level retention contract
# ---------------------------------------------------------------------------


class TestSessionLevelRetention:
    """One entry per session, not per turn."""

    def test_multi_turn_session_becomes_one_entry(self, tmp_path: Path) -> None:
        path = _write_fixture(tmp_path, [{
            "question_id": "q1",
            "question_type": "single-session-user",
            "question": "what did Alice say?",
            "answer": "she said hi",
            "haystack_session_ids": ["s1"],
            "haystack_sessions": [[
                {"role": "user", "content": "Alice: hi"},
                {"role": "assistant", "content": "responder: hello"},
                {"role": "user", "content": "Alice: how are you?"},
            ]],
            "haystack_dates": ["2024/01/15 (Mon) 10:00"],
        }])
        questions = load_longmemeval_dataset(path)

        assert len(questions) == 1
        # Previously: 3 entries (one per turn). After fix: 1.
        assert len(questions[0].conversation_context) == 1

        entry = questions[0].conversation_context[0]
        # All turn content present.
        assert "Alice: hi" in entry["content"]
        assert "responder: hello" in entry["content"]
        assert "Alice: how are you?" in entry["content"]
        # Role marker flipped to "session" so it's clear this isn't a
        # single-speaker utterance.
        assert entry["role"] == "session"
        assert entry["session_id"] == "s1"
        assert entry["session_date"] == "2024/01/15 (Mon) 10:00"

    def test_multiple_sessions_produce_separate_entries(self, tmp_path: Path) -> None:
        path = _write_fixture(tmp_path, [{
            "question_id": "q1",
            "question_type": "multi-session",
            "question": "what happened?",
            "answer": "various things",
            "haystack_session_ids": ["s1", "s2"],
            "haystack_sessions": [
                [{"role": "user", "content": "session one content"}],
                [{"role": "user", "content": "session two content"}],
            ],
            "haystack_dates": ["2024/01/15 (Mon) 10:00", "2024/01/16 (Tue) 11:00"],
        }])
        questions = load_longmemeval_dataset(path)

        # Two sessions → two entries; distinct session_ids.
        assert len(questions[0].conversation_context) == 2
        session_ids = {e["session_id"] for e in questions[0].conversation_context}
        assert session_ids == {"s1", "s2"}

        # Session dates propagated to the right entry.
        date_by_id = {
            e["session_id"]: e.get("session_date")
            for e in questions[0].conversation_context
        }
        assert date_by_id["s1"] == "2024/01/15 (Mon) 10:00"
        assert date_by_id["s2"] == "2024/01/16 (Tue) 11:00"

    def test_empty_session_is_dropped(self, tmp_path: Path) -> None:
        """A session with no non-empty turns must not produce an entry —
        otherwise the retain loop would see an empty content string."""
        path = _write_fixture(tmp_path, [{
            "question_id": "q1",
            "question_type": "single-session-user",
            "question": "q",
            "answer": "a",
            "haystack_session_ids": ["s1", "s2"],
            "haystack_sessions": [
                [],  # empty
                [{"role": "user", "content": "real content"}],
            ],
        }])
        questions = load_longmemeval_dataset(path)
        assert len(questions[0].conversation_context) == 1
        assert questions[0].conversation_context[0]["session_id"] == "s2"

    def test_turn_with_empty_content_skipped_but_session_retained(self, tmp_path: Path) -> None:
        """Individual empty turns should be skipped; the rest of the
        session still gets concatenated into one entry."""
        path = _write_fixture(tmp_path, [{
            "question_id": "q1",
            "question_type": "single-session-user",
            "question": "q",
            "answer": "a",
            "haystack_session_ids": ["s1"],
            "haystack_sessions": [[
                {"role": "user", "content": ""},           # empty — skip
                {"role": "user", "content": "good turn"},  # keep
                {"role": "assistant", "content": None},    # None — skip
                {"role": "assistant", "content": "also good"},
            ]],
        }])
        questions = load_longmemeval_dataset(path)
        entry = questions[0].conversation_context[0]
        assert "good turn" in entry["content"]
        assert "also good" in entry["content"]
        # Two turn-lines, separated by newline.
        assert entry["content"].count("\n") == 1

    def test_role_preserved_in_turn_markers(self, tmp_path: Path) -> None:
        """Turn-level `user:` / `assistant:` prefixes remain in the
        concatenated content so downstream reflect can still attribute
        who said what."""
        path = _write_fixture(tmp_path, [{
            "question_id": "q1",
            "question_type": "single-session-user",
            "question": "q",
            "answer": "a",
            "haystack_session_ids": ["s1"],
            "haystack_sessions": [[
                {"role": "user", "content": "my question"},
                {"role": "assistant", "content": "my answer"},
            ]],
        }])
        entry = load_longmemeval_dataset(path)[0].conversation_context[0]
        assert entry["content"].startswith("user: my question")
        assert "assistant: my answer" in entry["content"]


# ---------------------------------------------------------------------------
# Regression pin against the pre-fix behavior
# ---------------------------------------------------------------------------


class TestPreFixBehaviorDoesNotRegress:
    """Directly pin the bug the fix addresses. A 4-turn session must not
    produce 4 entries — if the loader ever reverts to per-turn emission,
    these tests fail loudly."""

    def test_per_turn_emission_does_not_return(self, tmp_path: Path) -> None:
        n_turns = 4
        path = _write_fixture(tmp_path, [{
            "question_id": "q1",
            "question_type": "single-session-user",
            "question": "q",
            "answer": "a",
            "haystack_session_ids": ["s1"],
            "haystack_sessions": [[
                {"role": "user", "content": f"turn {i}"} for i in range(n_turns)
            ]],
        }])
        questions = load_longmemeval_dataset(path)
        assert len(questions[0].conversation_context) == 1  # NOT n_turns
        # All turns still recoverable from the concatenated content.
        for i in range(n_turns):
            assert f"turn {i}" in questions[0].conversation_context[0]["content"]

    def test_session_count_matches_haystack_count(self, tmp_path: Path) -> None:
        """Number of context entries per question must equal number of
        haystack_sessions, not number of turns."""
        path = _write_fixture(tmp_path, [{
            "question_id": "q1",
            "question_type": "multi-session",
            "question": "q",
            "answer": "a",
            "haystack_session_ids": [f"s{i}" for i in range(5)],
            "haystack_sessions": [
                [{"role": "user", "content": "a"},
                 {"role": "user", "content": "b"}]
                for _ in range(5)
            ],
        }])
        questions = load_longmemeval_dataset(path)
        # 5 sessions × 2 turns = 10 turns, but only 5 entries.
        assert len(questions[0].conversation_context) == 5


# ---------------------------------------------------------------------------
# Date parser — stability check
# ---------------------------------------------------------------------------


class TestDateParser:
    def test_parses_canonical_longmemeval_format(self) -> None:
        dt = _parse_longmemeval_date("2024/01/15 (Mon) 10:00")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 10

    def test_tolerates_missing_weekday(self) -> None:
        dt = _parse_longmemeval_date("2024/01/15 10:00")
        assert dt is not None
        assert dt.year == 2024

    def test_returns_none_on_garbage(self) -> None:
        assert _parse_longmemeval_date("not a date") is None
        assert _parse_longmemeval_date("") is None
        assert _parse_longmemeval_date(None) is None
