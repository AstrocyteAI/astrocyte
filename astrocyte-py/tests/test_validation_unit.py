"""Unit tests for _validation module — bank_id edge cases."""

from __future__ import annotations

import pytest

from astrocyte._validation import validate_bank_id
from astrocyte.errors import ConfigError


class TestValidateBankId:
    def test_valid_simple(self) -> None:
        validate_bank_id("user-123")

    def test_valid_with_dots(self) -> None:
        validate_bank_id("org.team.bank")

    def test_valid_with_colons(self) -> None:
        validate_bank_id("user:alice:prefs")

    def test_valid_with_at(self) -> None:
        validate_bank_id("user@domain")

    def test_valid_single_char(self) -> None:
        validate_bank_id("a")

    def test_valid_numeric_start(self) -> None:
        validate_bank_id("1bank")

    def test_empty_raises(self) -> None:
        with pytest.raises(ConfigError, match="Invalid bank_id"):
            validate_bank_id("")

    def test_starts_with_dash_raises(self) -> None:
        with pytest.raises(ConfigError, match="Invalid bank_id"):
            validate_bank_id("-invalid")

    def test_starts_with_dot_raises(self) -> None:
        with pytest.raises(ConfigError, match="Invalid bank_id"):
            validate_bank_id(".invalid")

    def test_space_raises(self) -> None:
        with pytest.raises(ConfigError, match="Invalid bank_id"):
            validate_bank_id("has space")

    def test_slash_raises(self) -> None:
        with pytest.raises(ConfigError, match="Invalid bank_id"):
            validate_bank_id("path/to/bank")

    def test_max_length_255(self) -> None:
        validate_bank_id("a" * 255)

    def test_exceeds_max_length_raises(self) -> None:
        with pytest.raises(ConfigError, match="Invalid bank_id"):
            validate_bank_id("a" * 256)

    def test_unicode_raises(self) -> None:
        with pytest.raises(ConfigError, match="Invalid bank_id"):
            validate_bank_id("bänk")

    def test_newline_raises(self) -> None:
        with pytest.raises(ConfigError, match="Invalid bank_id"):
            validate_bank_id("bank\nid")
