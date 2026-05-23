"""Tests for ``astrocyte.pipeline._json_tolerant``.

Pinned behaviour:
- Straight valid JSON returns parsed object.
- ``json``-tagged and bare markdown fences are stripped.
- Leading and trailing prose is sliced off.
- Truncated trailing-comma JSON is unrecoverable → ``None``.
- Empty / non-JSON garbage → ``None``.
- ``looks_truncated`` flags unbalanced braces, trailing comma/colon,
  and non-terminal last chars so the caller skips the retry path
  when a retry can't help.
"""

from __future__ import annotations

from astrocyte.pipeline._json_tolerant import looks_truncated, tolerant_json_loads


class TestTolerantJsonLoads:
    def test_plain_json_object(self) -> None:
        assert tolerant_json_loads('{"a": 1}') == {"a": 1}

    def test_plain_json_array(self) -> None:
        assert tolerant_json_loads('[1, 2, 3]') == [1, 2, 3]

    def test_json_markdown_fence(self) -> None:
        text = '```json\n{"facts": [{"text": "x"}]}\n```'
        assert tolerant_json_loads(text) == {"facts": [{"text": "x"}]}

    def test_bare_markdown_fence(self) -> None:
        text = '```\n{"entities": ["Alice"]}\n```'
        assert tolerant_json_loads(text) == {"entities": ["Alice"]}

    def test_leading_and_trailing_prose(self) -> None:
        text = 'Sure, here is the JSON: {"facts": []} hope that helps!'
        assert tolerant_json_loads(text) == {"facts": []}

    def test_leading_prose_only(self) -> None:
        text = 'Here you go: {"a": 1, "b": 2}'
        assert tolerant_json_loads(text) == {"a": 1, "b": 2}

    def test_truncated_trailing_comma_unrecoverable(self) -> None:
        text = '{"facts": [{"text": "one"},'
        assert tolerant_json_loads(text) is None

    def test_empty_string(self) -> None:
        assert tolerant_json_loads("") is None

    def test_whitespace_only(self) -> None:
        assert tolerant_json_loads("   \n  ") is None

    def test_total_garbage(self) -> None:
        assert tolerant_json_loads("not json at all") is None


class TestLooksTruncated:
    def test_balanced_object_not_truncated(self) -> None:
        assert looks_truncated('{"a": 1}') is False

    def test_balanced_array_not_truncated(self) -> None:
        assert looks_truncated('[1, 2, 3]') is False

    def test_trailing_comma_is_truncated(self) -> None:
        assert looks_truncated('{"facts": [{"text": "x"},') is True

    def test_trailing_colon_is_truncated(self) -> None:
        assert looks_truncated('{"facts":') is True

    def test_unbalanced_braces_is_truncated(self) -> None:
        assert looks_truncated('{"facts": [{"text": "x"}') is True

    def test_mid_string_is_truncated(self) -> None:
        # Last char is a non-terminal letter — the model was clearly
        # writing more when it got cut off.
        assert looks_truncated('{"facts": [{"text": "hello wor') is True

    def test_empty_not_truncated(self) -> None:
        assert looks_truncated("") is False
