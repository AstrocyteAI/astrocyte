"""Unit tests for OutputScanner — DLP scanning of recall/reflect output.

Tests the extracted module directly, without wiring up the full Astrocyte.
"""

from __future__ import annotations

from astrocyte._output_scanner import OutputScanner
from astrocyte.config import AstrocyteConfig, DlpConfig
from astrocyte.policy.observability import StructuredLogger
from astrocyte.types import MemoryHit, RecallResult, ReflectResult


def _make_scanner(
    *,
    scan_recall: bool = True,
    scan_reflect: bool = True,
    action: str = "redact",
) -> OutputScanner:
    config = AstrocyteConfig()
    config.dlp = DlpConfig(
        scan_recall_output=scan_recall,
        scan_reflect_output=scan_reflect,
        output_pii_action=action,
    )
    logger = StructuredLogger(level="WARNING")
    return OutputScanner(config, logger)


def _hit(text: str, score: float = 0.9, bank_id: str = "b1") -> MemoryHit:
    return MemoryHit(text=text, score=score, bank_id=bank_id)


class TestOutputScannerRecall:
    def test_redact_replaces_email(self) -> None:
        scanner = _make_scanner(action="redact")
        result = RecallResult(
            hits=[_hit("Contact john@example.com for info")],
            total_available=1,
            truncated=False,
        )
        scanned = scanner.scan_recall(result)
        assert "john@example.com" not in scanned.hits[0].text
        assert "[EMAIL_REDACTED]" in scanned.hits[0].text

    def test_reject_drops_pii_hits(self) -> None:
        scanner = _make_scanner(action="reject")
        result = RecallResult(
            hits=[
                _hit("Call 555-123-4567"),
                _hit("No PII here"),
            ],
            total_available=2,
            truncated=False,
        )
        scanned = scanner.scan_recall(result)
        assert len(scanned.hits) == 1
        assert scanned.hits[0].text == "No PII here"

    def test_warn_passes_through(self) -> None:
        scanner = _make_scanner(action="warn")
        result = RecallResult(
            hits=[_hit("Email is test@test.com")],
            total_available=1,
            truncated=False,
        )
        scanned = scanner.scan_recall(result)
        assert scanned.hits[0].text == "Email is test@test.com"

    def test_no_pii_unchanged(self) -> None:
        scanner = _make_scanner(action="redact")
        result = RecallResult(
            hits=[_hit("The sky is blue")],
            total_available=1,
            truncated=False,
        )
        scanned = scanner.scan_recall(result)
        assert scanned.hits[0].text == "The sky is blue"

    def test_preserves_metadata_fields(self) -> None:
        scanner = _make_scanner(action="redact")
        hit = MemoryHit(
            text="Email john@example.com",
            score=0.95,
            bank_id="bank-x",
            fact_type="contact",
            tags=["important"],
            source="chat",
            memory_id="mem-1",
            memory_layer="semantic",
            utility_score=0.8,
        )
        result = RecallResult(hits=[hit], total_available=1, truncated=False)
        scanned = scanner.scan_recall(result)
        s = scanned.hits[0]
        assert s.score == 0.95
        assert s.bank_id == "bank-x"
        assert s.fact_type == "contact"
        assert s.tags == ["important"]
        assert s.source == "chat"
        assert s.memory_id == "mem-1"
        assert s.memory_layer == "semantic"
        assert s.utility_score == 0.8

    def test_preserves_total_available_and_truncated(self) -> None:
        scanner = _make_scanner(action="reject")
        result = RecallResult(
            hits=[_hit("SSN 123-45-6789")],
            total_available=5,
            truncated=True,
        )
        scanned = scanner.scan_recall(result)
        assert scanned.total_available == 5
        assert scanned.truncated is True

    def test_disabled_scanner_passthrough(self) -> None:
        scanner = _make_scanner(scan_recall=False, scan_reflect=False)
        assert not scanner.has_scanner
        result = RecallResult(
            hits=[_hit("Email john@example.com")],
            total_available=1,
            truncated=False,
        )
        scanned = scanner.scan_recall(result)
        assert scanned.hits[0].text == "Email john@example.com"

    def test_empty_hits(self) -> None:
        scanner = _make_scanner(action="redact")
        result = RecallResult(hits=[], total_available=0, truncated=False)
        scanned = scanner.scan_recall(result)
        assert scanned.hits == []


class TestOutputScannerReflect:
    def test_redact_replaces_email_in_answer(self) -> None:
        scanner = _make_scanner(action="redact")
        result = ReflectResult(
            answer="The email is user@domain.org for contact.",
            sources=[],
        )
        scanned = scanner.scan_reflect(result)
        assert "user@domain.org" not in scanned.answer
        assert "[EMAIL_REDACTED]" in scanned.answer

    def test_reject_returns_empty_answer_with_observation(self) -> None:
        scanner = _make_scanner(action="reject")
        result = ReflectResult(
            answer="SSN is 123-45-6789",
            confidence=0.9,
            sources=[_hit("source")],
        )
        scanned = scanner.scan_reflect(result)
        assert scanned.answer == ""
        assert scanned.sources == [_hit("source")]
        assert any("DLP" in o for o in (scanned.observations or []))

    def test_warn_passes_through(self) -> None:
        scanner = _make_scanner(action="warn")
        result = ReflectResult(answer="Call 555-123-4567", sources=[])
        scanned = scanner.scan_reflect(result)
        assert scanned.answer == "Call 555-123-4567"

    def test_no_pii_unchanged(self) -> None:
        scanner = _make_scanner(action="redact")
        result = ReflectResult(answer="The weather is sunny.", sources=[])
        scanned = scanner.scan_reflect(result)
        assert scanned.answer == "The weather is sunny."

    def test_preserves_confidence_on_redact(self) -> None:
        scanner = _make_scanner(action="redact")
        result = ReflectResult(
            answer="Email user@test.com",
            confidence=0.85,
            sources=[],
            observations=["existing observation"],
        )
        scanned = scanner.scan_reflect(result)
        assert scanned.confidence == 0.85
        assert scanned.observations == ["existing observation"]

    def test_disabled_scanner_passthrough(self) -> None:
        scanner = _make_scanner(scan_recall=False, scan_reflect=False)
        result = ReflectResult(answer="SSN 123-45-6789", sources=[])
        scanned = scanner.scan_reflect(result)
        assert scanned.answer == "SSN 123-45-6789"
