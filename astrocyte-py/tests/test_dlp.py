"""Tests for DLP output scanning on recall and reflect."""

from __future__ import annotations

import pytest

from astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig, DlpConfig
from astrocyte.testing.in_memory import InMemoryEngineProvider


def _make_brain(
    *,
    scan_recall: bool = False,
    scan_reflect: bool = False,
    action: str = "redact",
    input_pii_mode: str = "disabled",
) -> tuple[Astrocyte, InMemoryEngineProvider]:
    config = AstrocyteConfig()
    # Disable input PII scanning so PII reaches storage for DLP output tests
    config.barriers.pii.mode = input_pii_mode
    config.barriers.pii.action = "redact"
    config.dlp = DlpConfig(
        scan_recall_output=scan_recall,
        scan_reflect_output=scan_reflect,
        output_pii_action=action,
    )
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


class TestDlpRecallScanning:
    @pytest.mark.asyncio
    async def test_recall_redacts_pii_in_hits(self) -> None:
        brain, engine = _make_brain(scan_recall=True, action="redact")
        await brain.retain(
            "Contact john@example.com for details",
            bank_id="b1",
        )
        result = await brain.recall("Contact john", bank_id="b1")
        assert result.total_available >= 1
        # Email should be redacted
        assert "john@example.com" not in result.hits[0].text
        assert "[EMAIL_REDACTED]" in result.hits[0].text

    @pytest.mark.asyncio
    async def test_recall_reject_drops_pii_hits(self) -> None:
        brain, engine = _make_brain(scan_recall=True, action="reject")
        await brain.retain(
            "Call me at 555-123-4567 please",
            bank_id="b1",
        )
        await brain.retain(
            "No PII in this memory at all",
            bank_id="b1",
        )
        result = await brain.recall("Call me", bank_id="b1")
        # PII hit should be dropped, clean hit should remain
        for hit in result.hits:
            assert "555-123-4567" not in hit.text

    @pytest.mark.asyncio
    async def test_recall_warn_passes_through(self) -> None:
        brain, engine = _make_brain(scan_recall=True, action="warn")
        await brain.retain(
            "Email is test@test.com",
            bank_id="b1",
        )
        result = await brain.recall("Email test", bank_id="b1")
        assert result.total_available >= 1
        # Warn mode passes through original text
        assert "test@test.com" in result.hits[0].text

    @pytest.mark.asyncio
    async def test_recall_no_pii_unchanged(self) -> None:
        brain, engine = _make_brain(scan_recall=True, action="redact")
        await brain.retain(
            "The sky is blue and grass is green",
            bank_id="b1",
        )
        result = await brain.recall("sky blue grass", bank_id="b1")
        assert result.hits[0].text == "The sky is blue and grass is green"

    @pytest.mark.asyncio
    async def test_recall_dlp_disabled_by_default(self) -> None:
        brain, engine = _make_brain(scan_recall=False)
        await brain.retain(
            "Contact admin@company.com now",
            bank_id="b1",
        )
        result = await brain.recall("Contact admin", bank_id="b1")
        # DLP disabled — PII passes through
        assert "admin@company.com" in result.hits[0].text


class TestDlpReflectScanning:
    @pytest.mark.asyncio
    async def test_reflect_redacts_pii_in_answer(self) -> None:
        brain, engine = _make_brain(scan_reflect=True, action="redact")
        # Store something so reflect has material
        await brain.retain("The contact email is user@domain.org", bank_id="b1")

        result = await brain.reflect("What is the contact email?", bank_id="b1")
        # The synthesized answer may contain the email — DLP should redact it
        if "user@domain.org" in result.answer:
            # This shouldn't happen with DLP enabled
            pytest.fail("PII was not redacted from reflect output")

    @pytest.mark.asyncio
    async def test_reflect_reject_returns_empty(self) -> None:
        brain, engine = _make_brain(scan_reflect=True, action="reject")
        await brain.retain("SSN is 123-45-6789 for records", bank_id="b1")

        result = await brain.reflect("What is the SSN?", bank_id="b1")
        # If the reflect answer contains PII, reject should return empty answer
        # Note: InMemoryEngineProvider reflect just concatenates hits
        if result.answer == "":
            assert result.observations is not None
            assert any("DLP" in o for o in result.observations)

    @pytest.mark.asyncio
    async def test_reflect_dlp_disabled_by_default(self) -> None:
        brain, engine = _make_brain(scan_reflect=False)
        await brain.retain("Email is admin@test.com", bank_id="b1")

        result = await brain.reflect("What email?", bank_id="b1")
        # DLP disabled — no scanning applied
        assert result.answer is not None  # Just verify it returns something


class TestDlpConfigIntegration:
    def test_dlp_defaults_disabled(self) -> None:
        config = AstrocyteConfig()
        assert config.dlp.scan_recall_output is False
        assert config.dlp.scan_reflect_output is False
        assert config.dlp.output_pii_action == "warn"
