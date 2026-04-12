"""Unit tests for policy/barriers.py — PII scanning, content validation, metadata sanitization.

Tests PiiScanner (regex mode), ContentValidator, MetadataSanitizer, and _luhn_check.
"""

from __future__ import annotations

import pytest

from astrocyte.errors import PiiRejected
from astrocyte.policy.barriers import (
    ContentValidator,
    MetadataSanitizer,
    PiiScanner,
    _luhn_check,
)

# ---------------------------------------------------------------------------
# Luhn check
# ---------------------------------------------------------------------------


class TestLuhnCheck:
    def test_valid_card_number(self):
        # Known valid Luhn: 4539148803436467
        assert _luhn_check("4539148803436467") is True

    def test_invalid_card_number(self):
        assert _luhn_check("1234567890123456") is False

    def test_too_short(self):
        assert _luhn_check("12345") is False

    def test_too_long(self):
        assert _luhn_check("1" * 20) is False

    def test_strips_non_digits(self):
        assert _luhn_check("4539-1488-0343-6467") is True


# ---------------------------------------------------------------------------
# PiiScanner — regex mode
# ---------------------------------------------------------------------------


class TestPiiScannerRegex:
    def test_detect_email(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("Contact john@example.com for details")
        assert any(m.pii_type == "email" for m in matches)

    def test_detect_phone(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("Call 555-123-4567")
        assert any(m.pii_type == "phone" for m in matches)

    def test_detect_ssn(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("SSN is 123-45-6789")
        assert any(m.pii_type == "ssn" for m in matches)

    def test_detect_credit_card_valid_luhn(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("Card: 4539 1488 0343 6467")
        assert any(m.pii_type == "credit_card" for m in matches)

    def test_reject_credit_card_invalid_luhn(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("Not a card: 1234 5678 9012 3456")
        assert not any(m.pii_type == "credit_card" for m in matches)

    def test_detect_ip_v4(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("Server at 192.168.1.1")
        assert any(m.pii_type == "ip_address" for m in matches)

    def test_detect_dob(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("Born: 1990-05-15")
        assert any(m.pii_type == "date_of_birth" for m in matches)

    def test_detect_iban(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("IBAN: DE89 3704 0044 0532 0130 00")
        assert any(m.pii_type == "iban" for m in matches)

    def test_no_pii(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("The weather is sunny today")
        assert matches == []

    def test_disabled_mode(self):
        scanner = PiiScanner(mode="disabled")
        matches = scanner.scan("john@example.com 555-123-4567")
        assert matches == []

    def test_multiple_pii_types(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("Email john@example.com, call 555-123-4567")
        types = {m.pii_type for m in matches}
        assert "email" in types
        assert "phone" in types


# ---------------------------------------------------------------------------
# PiiScanner — country patterns
# ---------------------------------------------------------------------------


class TestPiiScannerCountry:
    def test_sg_nric(self):
        scanner = PiiScanner(mode="regex", countries=["SG"])
        matches = scanner.scan("NRIC: S1234567D")
        assert any(m.pii_type == "nric" for m in matches)

    def test_in_pan(self):
        scanner = PiiScanner(mode="regex", countries=["IN"])
        matches = scanner.scan("PAN: ABCDE1234F")
        assert any(m.pii_type == "pan" for m in matches)

    def test_uk_nino(self):
        scanner = PiiScanner(mode="regex", countries=["UK"])
        matches = scanner.scan("NI number: AB 12 34 56 A")
        assert any(m.pii_type == "uk_nino" for m in matches)

    def test_country_not_loaded_by_default(self):
        scanner = PiiScanner(mode="regex")  # No countries specified
        matches = scanner.scan("NRIC: S1234567D")
        assert not any(m.pii_type == "nric" for m in matches)


# ---------------------------------------------------------------------------
# PiiScanner — custom patterns
# ---------------------------------------------------------------------------


class TestPiiScannerCustom:
    def test_custom_pattern(self):
        scanner = PiiScanner(
            mode="regex",
            custom_patterns={"badge": (r"BADGE-\d{6}", "[BADGE_REDACTED]")},
        )
        matches = scanner.scan("Employee BADGE-123456")
        assert any(m.pii_type == "badge" for m in matches)
        assert matches[0].replacement == "[BADGE_REDACTED]"


# ---------------------------------------------------------------------------
# PiiScanner — apply actions
# ---------------------------------------------------------------------------


class TestPiiScannerApply:
    def test_redact_replaces_pii(self):
        scanner = PiiScanner(mode="regex", action="redact")
        text, matches = scanner.apply("Email john@example.com")
        assert "john@example.com" not in text
        assert "[EMAIL_REDACTED]" in text
        assert len(matches) > 0

    def test_reject_raises(self):
        scanner = PiiScanner(mode="regex", action="reject")
        with pytest.raises(PiiRejected):
            scanner.apply("Email john@example.com")

    def test_warn_passes_through(self):
        scanner = PiiScanner(mode="regex", action="warn")
        text, matches = scanner.apply("Email john@example.com")
        assert "john@example.com" in text
        assert len(matches) > 0

    def test_no_pii_no_change(self):
        scanner = PiiScanner(mode="regex", action="redact")
        text, matches = scanner.apply("No PII here")
        assert text == "No PII here"
        assert matches == []

    def test_redact_multiple(self):
        scanner = PiiScanner(mode="regex", action="redact")
        text, _ = scanner.apply("a@b.com and 555-123-4567")
        assert "[EMAIL_REDACTED]" in text
        assert "[PHONE_REDACTED]" in text

    def test_type_override_reject(self):
        scanner = PiiScanner(
            mode="regex",
            action="warn",
            type_overrides={"email": {"action": "reject"}},
        )
        with pytest.raises(PiiRejected):
            scanner.apply("john@example.com")

    def test_type_override_redact(self):
        scanner = PiiScanner(
            mode="regex",
            action="warn",
            type_overrides={"email": {"action": "redact"}},
        )
        text, matches = scanner.apply("john@example.com")
        assert "[EMAIL_REDACTED]" in text


# ---------------------------------------------------------------------------
# PiiScanner — async
# ---------------------------------------------------------------------------


class TestPiiScannerAsync:
    @pytest.mark.asyncio
    async def test_scan_async_regex(self):
        scanner = PiiScanner(mode="regex")
        matches = await scanner.scan_async("john@example.com")
        assert any(m.pii_type == "email" for m in matches)

    @pytest.mark.asyncio
    async def test_apply_async_redact(self):
        scanner = PiiScanner(mode="regex", action="redact")
        text, matches = await scanner.apply_async("john@example.com")
        assert "[EMAIL_REDACTED]" in text

    @pytest.mark.asyncio
    async def test_scan_async_disabled(self):
        scanner = PiiScanner(mode="disabled")
        matches = await scanner.scan_async("john@example.com")
        assert matches == []


# ---------------------------------------------------------------------------
# PiiScanner — merge_matches
# ---------------------------------------------------------------------------


class TestMergeMatches:
    def test_non_overlapping_kept(self):
        from astrocyte.types import PiiMatch

        a = [PiiMatch(pii_type="email", start=0, end=10, matched_text="a@b.com")]
        b = [PiiMatch(pii_type="phone", start=15, end=27, matched_text="555-123-4567")]
        merged = PiiScanner._merge_matches(a, b)
        assert len(merged) == 2

    def test_overlapping_deduped(self):
        from astrocyte.types import PiiMatch

        a = [PiiMatch(pii_type="email", start=0, end=15, matched_text="john@example.com")]
        b = [PiiMatch(pii_type="email", start=5, end=15, matched_text="@example.com")]
        merged = PiiScanner._merge_matches(a, b)
        assert len(merged) == 1  # Only the first (longer/earlier) kept


# ---------------------------------------------------------------------------
# ContentValidator
# ---------------------------------------------------------------------------


class TestContentValidator:
    def test_valid_content(self):
        cv = ContentValidator()
        errors = cv.validate("Hello world")
        assert errors == []

    def test_empty_content_rejected(self):
        cv = ContentValidator(reject_empty=True)
        errors = cv.validate("")
        assert len(errors) == 1
        assert "empty" in errors[0].lower()

    def test_empty_content_allowed(self):
        cv = ContentValidator(reject_empty=False)
        errors = cv.validate("")
        assert errors == []

    def test_too_long(self):
        cv = ContentValidator(max_content_length=10)
        errors = cv.validate("a" * 20)
        assert len(errors) == 1
        assert "too long" in errors[0].lower()

    def test_invalid_content_type(self):
        cv = ContentValidator()
        errors = cv.validate("hello", content_type="video")
        assert len(errors) == 1
        assert "not allowed" in errors[0].lower()

    def test_valid_content_type(self):
        cv = ContentValidator()
        for ct in ["text", "conversation", "document"]:
            assert cv.validate("hello", content_type=ct) == []

    def test_custom_allowed_types(self):
        cv = ContentValidator(allowed_content_types=["custom"])
        assert cv.validate("hello", content_type="custom") == []
        assert len(cv.validate("hello", content_type="text")) > 0

    def test_multiple_errors(self):
        cv = ContentValidator(max_content_length=5)
        errors = cv.validate("", content_type="invalid")
        assert len(errors) >= 2  # empty + invalid type


# ---------------------------------------------------------------------------
# MetadataSanitizer
# ---------------------------------------------------------------------------


class TestMetadataSanitizer:
    def test_none_metadata(self):
        ms = MetadataSanitizer()
        cleaned, warnings = ms.sanitize(None)
        assert cleaned is None
        assert warnings == []

    def test_clean_metadata_passes(self):
        ms = MetadataSanitizer()
        cleaned, warnings = ms.sanitize({"name": "Alice", "role": "admin"})
        assert cleaned == {"name": "Alice", "role": "admin"}
        assert warnings == []

    def test_blocks_sensitive_keys(self):
        ms = MetadataSanitizer()
        cleaned, warnings = ms.sanitize({
            "name": "Alice",
            "api_key": "sk-123",
            "password": "secret",
        })
        assert "api_key" not in cleaned
        assert "password" not in cleaned
        assert "name" in cleaned
        assert len(warnings) == 2

    def test_blocks_compound_sensitive_keys(self):
        ms = MetadataSanitizer()
        cleaned, warnings = ms.sanitize({
            "db_password": "x",
            "auth_token": "y",
            "my-secret": "z",
            "safe_key": "keep",
        })
        assert cleaned is not None
        assert "db_password" not in cleaned
        assert "auth_token" not in cleaned
        assert "my-secret" not in cleaned
        assert "safe_key" in cleaned

    def test_custom_blocked_keys(self):
        ms = MetadataSanitizer(blocked_keys=["internal"])
        cleaned, warnings = ms.sanitize({"internal": "val", "name": "ok"})
        assert "internal" not in cleaned
        assert "name" in cleaned

    def test_size_limit_truncates(self):
        ms = MetadataSanitizer(max_size_bytes=50)
        big = {f"key{i}": f"value{i}" for i in range(20)}
        cleaned, warnings = ms.sanitize(big)
        assert any("truncated" in w.lower() for w in warnings)

    def test_empty_metadata_returns_none(self):
        ms = MetadataSanitizer()
        cleaned, _ = ms.sanitize({"api_key": "secret"})
        assert cleaned is None  # Only key was blocked → empty → None


# ---------------------------------------------------------------------------
# PiiScanner — Unicode / non-ASCII patterns
# ---------------------------------------------------------------------------


class TestPiiScannerUnicode:
    """Ensure PII detection handles non-ASCII text without false positives/negatives."""

    def test_email_in_unicode_context(self):
        """PII email detection works when surrounded by non-ASCII text."""
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("Kontakt: john@example.com für Details")
        assert any(m.pii_type == "email" for m in matches)

    def test_phone_with_unicode_prefix(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("Téléphone: 555-123-4567")
        assert any(m.pii_type == "phone" for m in matches)

    def test_no_false_positive_on_cjk_digits(self):
        """CJK text with digits should not trigger SSN/phone false positives."""
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("日本語テスト 東京都港区")
        assert matches == []

    def test_ssn_in_mixed_unicode_context(self):
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("Numéro: 123-45-6789 résultat")
        assert any(m.pii_type == "ssn" for m in matches)

    def test_email_with_idn_domain(self):
        """Email with international domain characters should still match."""
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("user@example.co.uk works fine")
        assert any(m.pii_type == "email" for m in matches)

    def test_redact_preserves_unicode(self):
        scanner = PiiScanner(mode="regex", action="redact")
        text, matches = scanner.apply("Cher ami, mon email: test@example.com merci")
        assert "test@example.com" not in text
        assert "Cher ami" in text
        assert "merci" in text

    def test_fullwidth_digits_detected(self):
        """Full-width digits match \\d in Python regex — phone PII is still caught."""
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("Phone: ５５５-１２３-４５６７")
        # Python \d matches Unicode digits, so full-width digits trigger phone regex
        assert any(m.pii_type == "phone" for m in matches)
