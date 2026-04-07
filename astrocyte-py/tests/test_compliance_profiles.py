"""Tests for compliance profiles (GDPR, HIPAA, PDPA)."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrocyte.config import load_config
from astrocyte.errors import ConfigError


def _write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "astrocyte.yaml"
    p.write_text(content)
    return p


class TestComplianceProfileLoading:
    def test_gdpr_profile_sets_pii_mode(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "compliance_profile: gdpr\n")
        config = load_config(p)
        assert config.barriers.pii.mode == "rules_then_llm"
        assert config.barriers.pii.action == "redact"

    def test_gdpr_profile_enables_lifecycle(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "compliance_profile: gdpr\n")
        config = load_config(p)
        assert config.lifecycle.enabled is True
        assert config.lifecycle.ttl.archive_after_days == 365
        assert config.lifecycle.ttl.delete_after_days == 730

    def test_gdpr_profile_enables_access_control(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "compliance_profile: gdpr\n")
        config = load_config(p)
        assert config.access_control.enabled is True
        assert config.access_control.default_policy == "deny"

    def test_gdpr_profile_enables_dlp(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "compliance_profile: gdpr\n")
        config = load_config(p)
        assert config.dlp.scan_reflect_output is True
        assert config.dlp.scan_recall_output is False
        assert config.dlp.output_pii_action == "redact"

    def test_hipaa_profile_rejects_pii(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "compliance_profile: hipaa\n")
        config = load_config(p)
        assert config.barriers.pii.mode == "rules_then_llm"
        assert config.barriers.pii.action == "reject"

    def test_hipaa_profile_scans_both_outputs(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "compliance_profile: hipaa\n")
        config = load_config(p)
        assert config.dlp.scan_recall_output is True
        assert config.dlp.scan_reflect_output is True
        assert config.dlp.output_pii_action == "reject"

    def test_hipaa_profile_7_year_retention(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "compliance_profile: hipaa\n")
        config = load_config(p)
        assert config.lifecycle.ttl.delete_after_days == 2555

    def test_pdpa_profile_5_year_retention(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "compliance_profile: pdpa\n")
        config = load_config(p)
        assert config.lifecycle.ttl.delete_after_days == 1825

    def test_pdpa_profile_owner_only(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "compliance_profile: pdpa\n")
        config = load_config(p)
        assert config.access_control.default_policy == "owner_only"


class TestComplianceProfileOverrides:
    def test_user_config_overrides_compliance(self, tmp_path: Path) -> None:
        """User's explicit settings win over compliance profile defaults."""
        content = """
compliance_profile: gdpr
barriers:
  pii:
    action: warn
lifecycle:
  ttl:
    delete_after_days: 999
"""
        p = _write_config(tmp_path, content)
        config = load_config(p)
        # User override wins
        assert config.barriers.pii.action == "warn"
        assert config.lifecycle.ttl.delete_after_days == 999
        # Compliance defaults still apply where not overridden
        assert config.barriers.pii.mode == "rules_then_llm"

    def test_behavior_profile_composes_with_compliance(self, tmp_path: Path) -> None:
        """Both profile and compliance_profile can be set."""
        content = """
profile: personal
compliance_profile: gdpr
"""
        p = _write_config(tmp_path, content)
        config = load_config(p)
        # Compliance sets lifecycle
        assert config.lifecycle.enabled is True
        # Personal profile sets empathy
        assert config.defaults.empathy == 4

    def test_no_compliance_profile_is_noop(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "profile: minimal\n")
        config = load_config(p)
        # DLP defaults to disabled
        assert config.dlp.scan_recall_output is False
        assert config.dlp.scan_reflect_output is False

    def test_invalid_compliance_profile_raises(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "compliance_profile: nonexistent\n")
        with pytest.raises(ConfigError, match="Compliance profile not found"):
            load_config(p)
