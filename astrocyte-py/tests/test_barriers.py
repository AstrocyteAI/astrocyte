"""Tests for policy/barriers.py — PII scanner, content validator, metadata sanitizer."""

import pytest

from astrocyte.errors import PiiRejected
from astrocyte.policy.barriers import ContentValidator, MetadataSanitizer, PiiScanner


class TestPiiScanner:
    def test_detects_email(self):
        scanner = PiiScanner()
        matches = scanner.scan("Contact me at user@example.com please")
        assert len(matches) >= 1
        assert any(m.pii_type == "email" for m in matches)

    def test_detects_phone(self):
        scanner = PiiScanner()
        matches = scanner.scan("Call me at 555-123-4567")
        assert any(m.pii_type == "phone" for m in matches)

    def test_detects_ssn(self):
        scanner = PiiScanner()
        matches = scanner.scan("My SSN is 123-45-6789")
        assert any(m.pii_type == "ssn" for m in matches)

    def test_detects_credit_card_with_luhn(self):
        scanner = PiiScanner()
        # Valid Luhn: 4111 1111 1111 1111
        matches = scanner.scan("Card: 4111 1111 1111 1111")
        assert any(m.pii_type == "credit_card" for m in matches)

    def test_rejects_invalid_credit_card(self):
        scanner = PiiScanner()
        # Invalid Luhn
        matches = scanner.scan("Card: 1234 5678 9012 3456")
        cc_matches = [m for m in matches if m.pii_type == "credit_card"]
        assert len(cc_matches) == 0

    def test_detects_ip_address(self):
        scanner = PiiScanner()
        matches = scanner.scan("Server at 192.168.1.100")
        assert any(m.pii_type == "ip_address" for m in matches)

    def test_redact_action(self):
        scanner = PiiScanner(action="redact")
        result, matches = scanner.apply("Email: user@example.com")
        assert "[EMAIL_REDACTED]" in result
        assert "user@example.com" not in result

    def test_reject_action(self):
        scanner = PiiScanner(action="reject")
        with pytest.raises(PiiRejected):
            scanner.apply("Email: user@example.com")

    def test_warn_action(self):
        scanner = PiiScanner(action="warn")
        result, matches = scanner.apply("Email: user@example.com")
        assert "user@example.com" in result  # Not redacted
        assert len(matches) >= 1  # But matches returned

    def test_disabled_mode(self):
        scanner = PiiScanner(mode="disabled")
        matches = scanner.scan("Email: user@example.com")
        assert matches == []

    def test_no_pii_returns_empty(self):
        scanner = PiiScanner()
        matches = scanner.scan("The weather is nice today")
        assert matches == []

    def test_multiple_pii_types(self):
        scanner = PiiScanner(action="redact")
        text = "Email: user@example.com, Phone: 555-123-4567, SSN: 123-45-6789"
        result, matches = scanner.apply(text)
        assert "[EMAIL_REDACTED]" in result
        assert "[PHONE_REDACTED]" in result
        assert "[SSN_REDACTED]" in result

    def test_custom_patterns(self):
        scanner = PiiScanner(custom_patterns={"custom_id": (r"CUST-\d{8}", "[CUSTOMER_REDACTED]")})
        matches = scanner.scan("Customer CUST-12345678")
        assert any(m.pii_type == "custom_id" for m in matches)


class TestContentValidator:
    def test_valid_content(self):
        validator = ContentValidator()
        errors = validator.validate("Hello world", "text")
        assert errors == []

    def test_empty_content(self):
        validator = ContentValidator(reject_empty=True)
        errors = validator.validate("", "text")
        assert len(errors) > 0
        assert "empty" in errors[0].lower()

    def test_whitespace_only_is_empty(self):
        validator = ContentValidator(reject_empty=True)
        errors = validator.validate("   \n\t  ", "text")
        assert len(errors) > 0

    def test_too_long(self):
        validator = ContentValidator(max_content_length=10)
        errors = validator.validate("a" * 20, "text")
        assert len(errors) > 0
        assert "too long" in errors[0].lower()

    def test_invalid_content_type(self):
        validator = ContentValidator(allowed_content_types=["text"])
        errors = validator.validate("hello", "binary")
        assert len(errors) > 0


class TestMetadataSanitizer:
    def test_strips_blocked_keys(self):
        sanitizer = MetadataSanitizer(blocked_keys=["secret", "password"])
        cleaned, warnings = sanitizer.sanitize({"name": "Calvin", "secret": "hidden"})
        assert "secret" not in cleaned
        assert "name" in cleaned
        assert len(warnings) > 0

    def test_partial_match_blocked(self):
        sanitizer = MetadataSanitizer(blocked_keys=["api_key"])
        cleaned, warnings = sanitizer.sanitize({"my_api_key": "hidden", "name": "Calvin"})
        assert "my_api_key" not in cleaned

    def test_none_metadata(self):
        sanitizer = MetadataSanitizer()
        cleaned, warnings = sanitizer.sanitize(None)
        assert cleaned is None
        assert warnings == []

    def test_empty_metadata(self):
        sanitizer = MetadataSanitizer()
        cleaned, warnings = sanitizer.sanitize({})
        assert cleaned is None  # Empty dict becomes None
        assert warnings == []

    def test_size_limit(self):
        sanitizer = MetadataSanitizer(max_size_bytes=50)
        big_meta = {f"key_{i}": f"value_{i}" for i in range(100)}
        cleaned, warnings = sanitizer.sanitize(big_meta)
        assert any("exceeds" in w.lower() or "truncated" in w.lower() for w in warnings)
