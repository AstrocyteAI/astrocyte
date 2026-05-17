"""Tests for bank disposition + background prompt-shaping."""

from __future__ import annotations

import pytest

from astrocyte.disposition import (
    DEFAULT_TRAIT,
    BankDisposition,
    BankProfile,
    describe_trait_level,
    format_background_block,
    format_disposition_block,
    format_profile_block,
    is_balanced,
)

# ─── BankDisposition validation ───────────────────────────────────────


class TestDispositionValidation:
    def test_default_is_balanced(self) -> None:
        d = BankDisposition()
        assert d.skepticism == 3
        assert d.literalism == 3
        assert d.empathy == 3

    def test_explicit_in_range(self) -> None:
        d = BankDisposition(skepticism=1, literalism=5, empathy=3)
        assert d.skepticism == 1
        assert d.literalism == 5
        assert d.empathy == 3

    @pytest.mark.parametrize("value", [0, 6, -1, 100])
    def test_out_of_range_raises(self, value: int) -> None:
        with pytest.raises(ValueError, match="must be in 1..5"):
            BankDisposition(skepticism=value)

    def test_wrong_type_raises(self) -> None:
        with pytest.raises(TypeError, match="must be int"):
            BankDisposition(skepticism="high")  # type: ignore[arg-type]

    def test_balanced_classmethod(self) -> None:
        d = BankDisposition.balanced()
        assert d.skepticism == d.literalism == d.empathy == 3


class TestDispositionSerialization:
    def test_to_dict_round_trip(self) -> None:
        d = BankDisposition(skepticism=2, literalism=4, empathy=5)
        data = d.to_dict()
        assert data == {"skepticism": 2, "literalism": 4, "empathy": 5}
        d2 = BankDisposition.from_dict(data)
        assert d2 == d

    def test_from_dict_missing_defaults(self) -> None:
        d = BankDisposition.from_dict({"skepticism": 5})
        assert d.skepticism == 5
        assert d.literalism == DEFAULT_TRAIT
        assert d.empathy == DEFAULT_TRAIT

    def test_from_dict_extras_ignored(self) -> None:
        d = BankDisposition.from_dict(
            {
                "skepticism": 2,
                "literalism": 3,
                "empathy": 4,
                "unknown_trait": 99,
            }
        )
        assert d.skepticism == 2

    def test_from_dict_empty(self) -> None:
        d = BankDisposition.from_dict({})
        assert d == BankDisposition.balanced()


# ─── BankProfile ──────────────────────────────────────────────────────


class TestBankProfile:
    def test_default_balanced_no_background(self) -> None:
        p = BankProfile()
        assert is_balanced(p.disposition)
        assert p.background == ""

    def test_explicit_background(self) -> None:
        p = BankProfile(background="a customer-support agent for clinics")
        assert "customer-support" in p.background

    def test_serialization_round_trip(self) -> None:
        p = BankProfile(
            disposition=BankDisposition(skepticism=4, literalism=2, empathy=3),
            background="tech support",
        )
        data = p.to_dict()
        p2 = BankProfile.from_dict(data)
        assert p2.disposition == p.disposition
        assert p2.background == p.background

    def test_from_dict_missing_fields(self) -> None:
        p = BankProfile.from_dict({})
        assert p == BankProfile()

    def test_from_dict_partial(self) -> None:
        p = BankProfile.from_dict({"background": "test"})
        assert p.background == "test"
        assert is_balanced(p.disposition)


# ─── trait-level descriptions ─────────────────────────────────────────


class TestTraitLevel:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (1, "very low"),
            (2, "low"),
            (3, "moderate"),
            (4, "high"),
            (5, "very high"),
        ],
    )
    def test_known_levels(self, value: int, expected: str) -> None:
        assert describe_trait_level(value) == expected

    def test_out_of_range_defaults_to_moderate(self) -> None:
        assert describe_trait_level(99) == "moderate"


# ─── prompt formatters ────────────────────────────────────────────────


class TestDispositionFormat:
    def test_default_renders_balanced_descriptions(self) -> None:
        d = BankDisposition.balanced()
        block = format_disposition_block(d)
        assert "Skepticism (moderate)" in block
        assert "Literalism (moderate)" in block
        assert "Empathy (moderate)" in block
        assert "balanced approach to information" in block

    def test_high_skepticism_renders_correctly(self) -> None:
        d = BankDisposition(skepticism=5, literalism=3, empathy=3)
        block = format_disposition_block(d)
        assert "Skepticism (very high)" in block
        assert "highly skeptical" in block

    def test_low_empathy_renders(self) -> None:
        d = BankDisposition(skepticism=3, literalism=3, empathy=1)
        block = format_disposition_block(d)
        assert "Empathy (very low)" in block
        assert "facts and data" in block


class TestBackgroundFormat:
    def test_empty_returns_empty_string(self) -> None:
        assert format_background_block("") == ""
        assert format_background_block("   ") == ""

    def test_non_empty_renders_block(self) -> None:
        out = format_background_block("a personal assistant")
        assert out.startswith("Background:")
        assert "personal assistant" in out

    def test_strips_whitespace(self) -> None:
        out = format_background_block("  test text  ")
        assert "test text" in out
        assert "  test text  " not in out


class TestProfileFormat:
    def test_disposition_only_when_no_background(self) -> None:
        p = BankProfile()
        out = format_profile_block(p)
        assert "Your disposition traits:" in out
        assert "Background:" not in out

    def test_both_blocks_when_background_present(self) -> None:
        p = BankProfile(background="lab assistant")
        out = format_profile_block(p)
        assert "Your disposition traits:" in out
        assert "Background:" in out
        assert "lab assistant" in out

    def test_blocks_separated_by_blank_line(self) -> None:
        p = BankProfile(background="x")
        out = format_profile_block(p)
        assert "\n\n" in out  # double newline between sections


# ─── is_balanced ──────────────────────────────────────────────────────


class TestIsBalanced:
    def test_default_is_balanced(self) -> None:
        assert is_balanced(BankDisposition())

    def test_non_default_not_balanced(self) -> None:
        assert not is_balanced(BankDisposition(skepticism=4))
        assert not is_balanced(BankDisposition(empathy=1))
