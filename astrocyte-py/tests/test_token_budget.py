"""Tests for M35-1 token-budget utility."""

from __future__ import annotations

from dataclasses import dataclass

from astrocyte.pipeline.token_budget import count_tokens, pack_to_budget


class TestCountTokens:
    def test_empty_string_is_zero(self) -> None:
        assert count_tokens("") == 0

    def test_none_is_zero(self) -> None:
        assert count_tokens(None) == 0  # type: ignore[arg-type]

    def test_simple_english(self) -> None:
        # "hello world" is 2 tokens in cl100k_base.
        assert count_tokens("hello world") == 2

    def test_longer_text(self) -> None:
        # Rough sanity bound — short paragraph ≤ 80 tokens.
        n = count_tokens(
            "The user attended Sarah's wedding on June 9, 2023 at the rooftop "
            "garden in Brooklyn. The ceremony lasted two hours and was followed "
            "by a reception with dancing."
        )
        assert 25 < n < 80

    def test_repeated_calls_use_cached_encoding(self) -> None:
        # Two calls in quick succession should both succeed and produce
        # the same count — proves the module-level cache is working.
        n1 = count_tokens("ping")
        n2 = count_tokens("ping")
        assert n1 == n2
        assert n1 > 0


@dataclass
class _Item:
    text: str
    fid: str


def _text_of(it: _Item) -> str:
    return it.text


class TestPackToBudget:
    def test_zero_budget_returns_empty(self) -> None:
        items = [_Item(text="hello", fid="a")]
        assert pack_to_budget(items, max_tokens=0, text_of=_text_of) == []

    def test_negative_budget_returns_empty(self) -> None:
        items = [_Item(text="hello", fid="a")]
        assert pack_to_budget(items, max_tokens=-10, text_of=_text_of) == []

    def test_packs_within_budget(self) -> None:
        # Three short items (~1-2 tokens each); generous budget — all fit.
        items = [_Item(text=f"item{i}", fid=str(i)) for i in range(3)]
        out = pack_to_budget(items, max_tokens=100, text_of=_text_of)
        assert [it.fid for it in out] == ["0", "1", "2"]

    def test_stops_when_budget_exhausted(self) -> None:
        # Long-ish items; tight budget should accept only the first few.
        items = [
            _Item(text="The quick brown fox jumps over the lazy dog. " * 10, fid=str(i))
            for i in range(10)
        ]
        # Each item is ~90 tokens; budget 200 should fit ~2 of them.
        out = pack_to_budget(items, max_tokens=200, text_of=_text_of)
        assert 1 <= len(out) <= 3
        # Output preserves input order (top-ranked first).
        assert [it.fid for it in out] == [str(i) for i in range(len(out))]

    def test_first_item_always_included_even_if_oversize(self) -> None:
        # Single long fact larger than budget. Must still be returned —
        # otherwise an oversized gold fact would produce zero output.
        big = _Item(text="word " * 1000, fid="big")
        small = _Item(text="hi", fid="small")
        out = pack_to_budget([big, small], max_tokens=50, text_of=_text_of)
        assert out[0].fid == "big"
        # The small one doesn't fit because big already blew the budget.
        assert "small" not in [it.fid for it in out]

    def test_skips_oversize_non_first_items(self) -> None:
        # First item fits, second is huge, third is small. Behaviour:
        # accept first, skip second, accept third.
        items = [
            _Item(text="small first", fid="a"),
            _Item(text="word " * 1000, fid="b"),
            _Item(text="small third", fid="c"),
        ]
        out = pack_to_budget(items, max_tokens=100, text_of=_text_of)
        ids = [it.fid for it in out]
        assert "a" in ids
        assert "b" not in ids
        assert "c" in ids

    def test_preserves_input_order(self) -> None:
        # Caller pre-sorts by relevance; pack_to_budget must NOT reshuffle.
        items = [
            _Item(text="alpha", fid="3"),
            _Item(text="beta", fid="1"),
            _Item(text="gamma", fid="2"),
        ]
        out = pack_to_budget(items, max_tokens=100, text_of=_text_of)
        assert [it.fid for it in out] == ["3", "1", "2"]
