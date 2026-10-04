"""A valid JSON answer followed by prose must not be lost.

Observed in benchmark logs (2026-10-04): claude_cli consolidation replies like
"```json\n[]\n```\n\nThe new memory is a status update [...]" were parsed
from the first "[" to the last "]", swallowing the prose and failing with
"Extra data". Every such reply silently dropped an observation update.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline._json_tolerant import first_json_value
from astrocyte.pipeline.observation import _parse_actions

FENCED_THEN_PROSE = "```json\n[]\n```\n\nThe new memory is a generic reply [not an observation] about tennis."
ACTION = '{"action": "create", "text": "User plays tennis on Sundays."}'


class TestFirstJsonValue:
    @pytest.mark.parametrize(
        ("text", "kind", "expected"),
        [
            ("[1, 2]", list, [1, 2]),
            (FENCED_THEN_PROSE, list, []),
            ("Sure! [1] is my answer, see [notes].", list, [1]),
            ('prefix {"a": 1} suffix {"b": 2}', dict, {"a": 1}),
            ('{"a": [1]}', list, [1]),  # first list found inside, when a list is asked for
            ("no json here", list, None),
            ("", list, None),
            ("[unclosed", list, None),
        ],
    )
    def test_cases(self, text, kind, expected):
        assert first_json_value(text, kind) == expected

    def test_fenced_block_wins_over_earlier_brackets_in_prose(self):
        text = "Reasoning [draft] first.\n```json\n[2]\n```"
        assert first_json_value(text, list) == [2]

    def test_bounded_scan_on_bracket_heavy_prose(self):
        assert first_json_value("[" * 10_000, list) is None


class TestParseActions:
    def test_fenced_answer_followed_by_bracketed_prose_is_kept(self):
        raw = f"```json\n[{ACTION}]\n```\n\nI created one observation [about tennis]."
        assert _parse_actions(raw) == [{"action": "create", "text": "User plays tennis on Sundays."}]

    def test_empty_array_with_trailing_prose_is_a_valid_no_op(self, caplog):
        assert _parse_actions(FENCED_THEN_PROSE) == []
        assert "parse error" not in caplog.text

    def test_invalid_actions_still_filtered(self):
        assert _parse_actions('[{"action": "explode"}, ' + ACTION + "]") == [
            {"action": "create", "text": "User plays tennis on Sundays."}
        ]
