"""Verbatim-extraction metadata must land on the chunk it describes.

The defect: ``extract_facts_verbatim`` paired the model's metadata entries
with chunks by list position. A paired experiment on 18 real LongMemEval
batches (2026-10-03) found the entry count was wrong in 25% of calls with
extended thinking on and 69% with it off. Entity grounding in the positional
chunk fell from 87.6% to 74.2%, and to 63.2% in the second half of each
list: one skipped or merged entry shifts every later chunk onto its
neighbour's dates and entities, with no error anywhere.

These pin the replacement join (the echoed ``chunk_index``) and the rule
that absent metadata beats misattributed metadata.
"""

from __future__ import annotations

import json

import pytest

from astrocyte.pipeline.fact_extraction import (
    _VERBATIM_JSON_SCHEMA,
    _VERBATIM_SYSTEM_PROMPT,
    align_verbatim_metadata,
    extract_facts_verbatim,
)
from astrocyte.types import Completion


def meta(i: int, **extra) -> dict:
    """An entry whose `when` names its own chunk, so misplacement is visible."""
    return {"chunk_index": i, "when": f"chunk-{i}", **extra}


class TestKeyedJoin:
    def test_skipped_middle_entry_does_not_shift_later_chunks(self):
        """THE regression. Positional join hands chunk 3 chunk 4's metadata, and so on."""
        entries = [meta(0), meta(1), meta(3), meta(4)]        # chunk 2 skipped
        out = align_verbatim_metadata(entries, 5)
        assert [o.get("when") for o in out] == ["chunk-0", "chunk-1", None, "chunk-3", "chunk-4"]

    def test_merged_extra_entry_does_not_shift(self):
        entries = [meta(0), meta(1), {"chunk_index": 1, "when": "dup"}, meta(2)]
        out = align_verbatim_metadata(entries, 3)
        assert [o.get("when") for o in out] == ["chunk-0", "chunk-1", "chunk-2"]

    def test_out_of_order_entries_are_placed_by_index(self):
        out = align_verbatim_metadata([meta(2), meta(0), meta(1)], 3)
        assert [o.get("when") for o in out] == ["chunk-0", "chunk-1", "chunk-2"]

    def test_duplicate_index_keeps_the_first(self):
        out = align_verbatim_metadata([meta(0), {"chunk_index": 0, "when": "second"}], 1)
        assert out[0]["when"] == "chunk-0"

    @pytest.mark.parametrize("bad", [-1, 7, None, True, "x", 1.5])
    def test_invalid_indices_are_discarded_not_guessed(self, bad):
        out = align_verbatim_metadata([{"chunk_index": bad, "when": "stray"}, meta(1)], 2)
        assert out[0] == {} and out[1]["when"] == "chunk-1"

    def test_numeric_string_index_is_accepted(self):
        out = align_verbatim_metadata([{"chunk_index": "1", "when": "s"}], 2)
        assert out[1]["when"] == "s"


class TestNoIndexFallback:
    def test_exact_length_without_indices_stays_positional(self):
        """Backward compatible: safe when the count is exact."""
        out = align_verbatim_metadata([{"when": "a"}, {"when": "b"}], 2)
        assert [o["when"] for o in out] == ["a", "b"]

    def test_wrong_length_without_indices_attaches_nothing(self):
        """Cannot locate the divergence, so do not guess."""
        out = align_verbatim_metadata([{"when": "a"}, {"when": "b"}], 3)
        assert out == [{}, {}, {}]


class TestPromptAndSchema:
    def test_schema_requires_chunk_index(self):
        item = _VERBATIM_JSON_SCHEMA["schema"]["properties"]["facts"]["items"]
        assert "chunk_index" in item["required"]
        assert item["properties"]["chunk_index"] == {"type": "integer"}

    def test_prompt_asks_for_the_index(self):
        assert "chunk_index" in _VERBATIM_SYSTEM_PROMPT


class _Provider:
    """Returns a canned response; skips chunk 1 to reproduce the failure."""

    def __init__(self, payload: dict):
        self.payload = payload

    async def complete(self, messages, **kwargs):
        return Completion(text=json.dumps(self.payload), model="fake", usage=None)


async def test_end_to_end_a_skipped_entry_costs_one_chunk_not_all_later_ones():
    chunks = ["Alice met Bob in Paris.", "The weather was mild.", "Carol flew to Tokyo."]
    payload = {"facts": [
        {**meta(0), "entities": [{"name": "Alice", "entity_type": "PERSON"}]},
        {**meta(2), "entities": [{"name": "Carol", "entity_type": "PERSON"}]},
    ]}
    facts = await extract_facts_verbatim(chunks, _Provider(payload))
    assert [f.what for f in facts] == chunks, "chunk text must always survive"
    assert [e.name for e in facts[0].entities] == ["Alice"]
    assert facts[1].entities == [], "the skipped chunk gets nothing, not Carol"
    assert [e.name for e in facts[2].entities] == ["Carol"], "Carol stays on her own chunk"
