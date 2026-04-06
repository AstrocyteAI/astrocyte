"""Tests for advanced PII detection — country-specific, NER, LLM, per-type overrides."""

from __future__ import annotations

import pytest

from astrocyte.errors import PiiRejected
from astrocyte.policy.barriers import PiiScanner
from astrocyte.policy.llm_scanner import _parse_llm_response

# ---------------------------------------------------------------------------
# Country-specific regex patterns
# ---------------------------------------------------------------------------


class TestSingaporePatterns:
    def test_nric(self) -> None:
        scanner = PiiScanner(countries=["SG"])
        matches = scanner.scan("My NRIC is S1234567D and that's private")
        assert any(m.pii_type == "nric" for m in matches)

    def test_sg_phone(self) -> None:
        scanner = PiiScanner(countries=["SG"])
        matches = scanner.scan("Call me at +65 9123 4567")
        assert any(m.pii_type == "sg_phone" for m in matches)


class TestIndiaPatterns:
    def test_pan(self) -> None:
        scanner = PiiScanner(countries=["IN"])
        matches = scanner.scan("PAN card number is ABCDE1234F")
        assert any(m.pii_type == "pan" for m in matches)

    def test_in_phone(self) -> None:
        scanner = PiiScanner(countries=["IN"])
        matches = scanner.scan("Reach me at +91 98765 43210")
        assert any(m.pii_type == "in_phone" for m in matches)


class TestUKPatterns:
    def test_uk_nino(self) -> None:
        scanner = PiiScanner(countries=["UK"])
        matches = scanner.scan("NI number: AB 12 34 56 C")
        assert any(m.pii_type == "uk_nino" for m in matches)

    def test_uk_phone(self) -> None:
        scanner = PiiScanner(countries=["UK"])
        matches = scanner.scan("Ring +44 2071 234567")
        assert any(m.pii_type == "uk_phone" for m in matches)


class TestEUPatterns:
    def test_it_codice_fiscale(self) -> None:
        scanner = PiiScanner(countries=["IT"])
        matches = scanner.scan("Codice fiscale: RSSMRA85M01H501Z")
        assert any(m.pii_type == "it_codice_fiscale" for m in matches)

    def test_es_dni(self) -> None:
        scanner = PiiScanner(countries=["ES"])
        matches = scanner.scan("DNI: 12345678Z")
        assert any(m.pii_type == "es_dni" for m in matches)

    def test_es_nie(self) -> None:
        scanner = PiiScanner(countries=["ES"])
        matches = scanner.scan("NIE: X1234567L")
        assert any(m.pii_type == "es_nie" for m in matches)


class TestAustraliaPatterns:
    def test_au_phone(self) -> None:
        scanner = PiiScanner(countries=["AU"])
        matches = scanner.scan("Contact +61 4 1234 5678")
        assert any(m.pii_type == "au_phone" for m in matches)


class TestChinaPatterns:
    def test_cn_resident_id(self) -> None:
        scanner = PiiScanner(countries=["CN"])
        # 18-digit Chinese citizen ID (fake but format-valid)
        matches = scanner.scan("ID: 110101199001011234")
        assert any(m.pii_type == "cn_resident_id" for m in matches)

    def test_cn_phone(self) -> None:
        scanner = PiiScanner(countries=["CN"])
        matches = scanner.scan("WeChat: +86 138 1234 5678")
        assert any(m.pii_type == "cn_phone" for m in matches)


class TestJapanPatterns:
    def test_jp_phone(self) -> None:
        scanner = PiiScanner(countries=["JP"])
        matches = scanner.scan("Phone: +81 3 1234 5678")
        assert any(m.pii_type == "jp_phone" for m in matches)


class TestGlobalPatterns:
    def test_date_of_birth(self) -> None:
        scanner = PiiScanner()
        matches = scanner.scan("Date of birth: 1990-01-15")
        assert any(m.pii_type == "date_of_birth" for m in matches)

    def test_iban(self) -> None:
        scanner = PiiScanner()
        matches = scanner.scan("IBAN: GB29 NWBK 6016 1331 9268 19")
        assert any(m.pii_type == "iban" for m in matches)


class TestMultiCountry:
    def test_all_countries_loaded(self) -> None:
        scanner = PiiScanner(countries=["SG", "IN", "UK", "DE", "FR", "IT", "ES", "AU", "CA", "JP", "CN"])
        # Should have global + all country patterns
        assert "nric" in scanner._patterns
        assert "pan" in scanner._patterns
        assert "uk_nino" in scanner._patterns
        assert "cn_resident_id" in scanner._patterns

    def test_unknown_country_ignored(self) -> None:
        scanner = PiiScanner(countries=["XX"])
        # Should still have global patterns
        assert "email" in scanner._patterns


# ---------------------------------------------------------------------------
# Per-type action overrides
# ---------------------------------------------------------------------------


class TestTypeOverrides:
    def test_reject_specific_type(self) -> None:
        scanner = PiiScanner(
            action="redact",
            type_overrides={"credit_card": {"action": "reject"}},
        )
        with pytest.raises(PiiRejected):
            scanner.apply("Card number 4111 1111 1111 1111")

    def test_redact_default_warn_override(self) -> None:
        scanner = PiiScanner(
            action="warn",
            type_overrides={"email": {"action": "redact"}},
        )
        result, matches = scanner.apply("Email is test@example.com")
        assert "[EMAIL_REDACTED]" in result

    def test_custom_replacement_in_override(self) -> None:
        scanner = PiiScanner(
            action="redact",
            type_overrides={"email": {"action": "redact", "replacement": "[HIDDEN]"}},
        )
        result, matches = scanner.apply("Contact user@domain.com")
        assert "[HIDDEN]" in result
        assert "user@domain.com" not in result


# ---------------------------------------------------------------------------
# LLM scanner parsing
# ---------------------------------------------------------------------------


class TestLlmParsing:
    def test_parse_valid_json(self) -> None:
        response = '[{"type": "name", "text": "John Smith", "start": 10, "end": 20}]'
        matches = _parse_llm_response(response, "I am John Smith and I live here")
        assert len(matches) == 1
        assert matches[0].pii_type == "name"
        assert matches[0].matched_text == "John Smith"

    def test_parse_code_block(self) -> None:
        response = '```json\n[{"type": "email", "text": "a@b.com", "start": 0, "end": 7}]\n```'
        matches = _parse_llm_response(response, "a@b.com is my email")
        assert len(matches) == 1

    def test_parse_invalid_json(self) -> None:
        matches = _parse_llm_response("not json", "some text")
        assert matches == []

    def test_parse_empty_array(self) -> None:
        matches = _parse_llm_response("[]", "clean text")
        assert matches == []

    def test_parse_finds_text_in_original(self) -> None:
        """When LLM doesn't provide offsets, find text in original."""
        response = '[{"type": "name", "text": "Alice"}]'
        matches = _parse_llm_response(response, "My friend Alice is here")
        assert len(matches) == 1
        assert matches[0].start == 10
        assert matches[0].end == 15


class TestLlmScanner:
    @pytest.mark.asyncio
    async def test_llm_scan_with_mock(self) -> None:
        from astrocyte.policy.llm_scanner import LlmPiiScanner
        from astrocyte.types import Completion, TokenUsage

        class MockLLM:
            async def complete(self, messages, **kwargs):
                return Completion(
                    text='[{"type": "medical_record", "text": "diagnosed with diabetes", "start": 15, "end": 38}]',
                    model="mock",
                    usage=TokenUsage(input_tokens=10, output_tokens=20),
                )

        scanner = LlmPiiScanner(MockLLM())
        matches = await scanner.scan("The patient was diagnosed with diabetes last year")
        assert len(matches) == 1
        assert matches[0].pii_type == "medical_record"

    @pytest.mark.asyncio
    async def test_llm_scan_failure_returns_empty(self) -> None:
        from astrocyte.policy.llm_scanner import LlmPiiScanner

        class FailingLLM:
            async def complete(self, messages, **kwargs):
                raise RuntimeError("LLM down")

        scanner = LlmPiiScanner(FailingLLM())
        matches = await scanner.scan("Some text with secrets")
        assert matches == []


# ---------------------------------------------------------------------------
# Mode wiring
# ---------------------------------------------------------------------------


class TestModeWiring:
    def test_disabled_returns_empty(self) -> None:
        scanner = PiiScanner(mode="disabled")
        assert scanner.scan("email@test.com SSN 123-45-6789") == []

    def test_regex_mode_default(self) -> None:
        scanner = PiiScanner(mode="regex")
        matches = scanner.scan("test@example.com")
        assert len(matches) >= 1

    @pytest.mark.asyncio
    async def test_scan_async_regex_mode(self) -> None:
        scanner = PiiScanner(mode="regex")
        matches = await scanner.scan_async("test@example.com")
        assert len(matches) >= 1

    @pytest.mark.asyncio
    async def test_rules_then_llm_regex_match_skips_llm(self) -> None:
        """When regex finds matches, LLM is not called."""
        from astrocyte.types import Completion, TokenUsage

        call_count = 0

        class TrackingLLM:
            async def complete(self, messages, **kwargs):
                nonlocal call_count
                call_count += 1
                return Completion(text="[]", model="mock", usage=TokenUsage(input_tokens=1, output_tokens=1))

        scanner = PiiScanner(mode="rules_then_llm", llm_provider=TrackingLLM())
        matches = await scanner.scan_async("Contact test@example.com")
        assert len(matches) >= 1
        assert call_count == 0  # LLM was not called

    @pytest.mark.asyncio
    async def test_rules_then_llm_no_regex_calls_llm(self) -> None:
        """When regex finds nothing, LLM is called."""
        from astrocyte.types import Completion, TokenUsage

        class MockLLM:
            async def complete(self, messages, **kwargs):
                return Completion(
                    text='[{"type": "name", "text": "Dr. Smith", "start": 4, "end": 13}]',
                    model="mock",
                    usage=TokenUsage(input_tokens=10, output_tokens=20),
                )

        scanner = PiiScanner(mode="rules_then_llm", llm_provider=MockLLM())
        matches = await scanner.scan_async("See Dr. Smith for your checkup")
        assert len(matches) >= 1
        assert matches[0].pii_type == "name"
