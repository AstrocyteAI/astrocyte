"""Tests for specific bugfixes — router logic, config merge order, edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig, load_config
from astrocyte.errors import AccessDenied
from astrocyte.mip.router import MipRouter
from astrocyte.mip.rule_engine import RuleEngineInput
from astrocyte.mip.schema import (
    ActionSpec,
    EscalationCondition,
    IntentPolicy,
    MatchBlock,
    MatchSpec,
    MipConfig,
    RoutingRule,
)
from astrocyte.testing.in_memory import InMemoryEngineProvider

# ---------------------------------------------------------------------------
# BUG 1+2: Router escalation for low-confidence matches
# ---------------------------------------------------------------------------


class TestRouterLowConfidence:
    def test_low_confidence_single_match_escalates(self) -> None:
        """A single match with confidence < 0.8 should escalate, not silently accept."""
        rules = [
            RoutingRule(
                name="weak-match",
                priority=10,
                match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="text")]),
                action=ActionSpec(bank="maybe-bank", confidence=0.5),
            ),
        ]
        config = MipConfig(
            rules=rules,
            intent_policy=IntentPolicy(
                escalate_when=[EscalationCondition(condition="confidence", operator="lt", value=0.8)],
            ),
        )
        router = MipRouter(config)
        decision = router.route_sync(RuleEngineInput(content="test", content_type="text"))
        # Low confidence + escalation policy → should return None (escalate)
        assert decision is None

    def test_high_confidence_single_match_accepted(self) -> None:
        """A single match with confidence >= 0.8 should be accepted."""
        rules = [
            RoutingRule(
                name="strong-match",
                priority=10,
                match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="text")]),
                action=ActionSpec(bank="correct-bank", confidence=0.95),
            ),
        ]
        router = MipRouter(MipConfig(rules=rules))
        decision = router.route_sync(RuleEngineInput(content="test", content_type="text"))
        assert decision is not None
        assert decision.bank_id == "correct-bank"

    def test_multiple_matches_with_escalation_policy(self) -> None:
        """Multiple conflicting matches should escalate when policy says so."""
        rules = [
            RoutingRule(
                name="rule-a",
                priority=10,
                match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="text")]),
                action=ActionSpec(bank="bank-a"),
            ),
            RoutingRule(
                name="rule-b",
                priority=20,
                match=MatchBlock(all_conditions=[MatchSpec(field="source", operator="eq", value="agent")]),
                action=ActionSpec(bank="bank-b"),
            ),
        ]
        config = MipConfig(
            rules=rules,
            intent_policy=IntentPolicy(
                escalate_when=[EscalationCondition(condition="conflicting_rules", operator="eq", value=True)],
            ),
        )
        router = MipRouter(config)
        decision = router.route_sync(RuleEngineInput(content="test", content_type="text", source="agent"))
        assert decision is None  # Conflicting → escalate

    def test_no_matches_returns_none(self) -> None:
        """No matches should always return None (no tautology)."""
        router = MipRouter(MipConfig(rules=[]))
        decision = router.route_sync(RuleEngineInput(content="test"))
        assert decision is None


# ---------------------------------------------------------------------------
# Config merge order: compliance < behavior profile < user config
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "astrocyte.yaml"
    p.write_text(content)
    return p


class TestConfigMergeOrder:
    def test_user_config_overrides_compliance_and_profile(self, tmp_path: Path) -> None:
        """User config should win over both compliance profile and behavior profile."""
        p = _write_config(
            tmp_path,
            """
profile: personal
compliance_profile: gdpr
barriers:
  pii:
    action: warn
""",
        )
        config = load_config(p)
        # User said "warn" — wins over GDPR's "redact" and personal's "redact"
        assert config.barriers.pii.action == "warn"

    def test_behavior_profile_overrides_compliance(self, tmp_path: Path) -> None:
        """Behavior profile settings should override compliance profile defaults."""
        # personal profile sets empathy=4; GDPR sets access_control.default_policy=deny
        p = _write_config(
            tmp_path,
            """
profile: personal
compliance_profile: gdpr
""",
        )
        config = load_config(p)
        # Personal profile's empathy wins (behavior > compliance)
        assert config.defaults.empathy == 4
        # GDPR's access control is still there (personal doesn't set it)
        assert config.access_control.enabled is True

    def test_compliance_only_applies_defaults(self, tmp_path: Path) -> None:
        """Compliance profile alone sets expected defaults."""
        p = _write_config(tmp_path, "compliance_profile: pdpa\n")
        config = load_config(p)
        assert config.lifecycle.ttl.delete_after_days == 1825
        assert config.access_control.default_policy == "owner_only"


# ---------------------------------------------------------------------------
# Legal hold bypass requires admin permission
# ---------------------------------------------------------------------------


class TestLegalHoldBypassAuth:
    @pytest.mark.asyncio
    async def test_compliance_forget_requires_admin(self, tmp_path: Path) -> None:
        """compliance=True bypass should require admin permission."""
        from astrocyte.types import AccessGrant, AstrocyteContext

        config = AstrocyteConfig()
        config.barriers.pii.mode = "disabled"
        config.access_control.enabled = True
        config.access_control.default_policy = "deny"

        brain = Astrocyte(config)
        engine = InMemoryEngineProvider()
        brain.set_engine_provider(engine)

        # Grant write+forget (but not admin) for setup
        brain.set_access_grants(
            [
                AccessGrant(bank_id="held-bank", principal="agent:basic", permissions=["read", "write", "forget"]),
            ]
        )

        ctx = AstrocyteContext(principal="agent:basic")
        await brain.retain("test data", bank_id="held-bank", context=ctx)
        brain.set_legal_hold("held-bank", "hold-1", "litigation")

        # compliance=True requires admin — agent:basic only has forget, not admin
        with pytest.raises(AccessDenied):
            await brain.forget(
                "held-bank",
                compliance=True,
                context=ctx,
            )

    @pytest.mark.asyncio
    async def test_compliance_forget_requires_context_even_without_acl(self, tmp_path: Path) -> None:
        """compliance=True without context should fail even when access_control is disabled."""
        config = AstrocyteConfig()
        config.barriers.pii.mode = "disabled"
        config.access_control.enabled = False  # ACL disabled

        brain = Astrocyte(config)
        engine = InMemoryEngineProvider()
        brain.set_engine_provider(engine)

        await brain.retain("test data", bank_id="held-bank")
        brain.set_legal_hold("held-bank", "hold-1", "litigation")

        # No context provided — should be denied even with ACL off
        with pytest.raises(AccessDenied):
            await brain.forget("held-bank", compliance=True)


# ---------------------------------------------------------------------------
# Config unknown keys don't crash
# ---------------------------------------------------------------------------


class TestConfigUnknownKeys:
    def test_unknown_keys_in_dlp_ignored(self, tmp_path: Path) -> None:
        p = _write_config(
            tmp_path,
            """
dlp:
  scan_recall_output: true
  future_unknown_key: true
""",
        )
        config = load_config(p)
        assert config.dlp.scan_recall_output is True

    def test_unknown_keys_in_lifecycle_ignored(self, tmp_path: Path) -> None:
        p = _write_config(
            tmp_path,
            """
lifecycle:
  enabled: true
  ttl:
    archive_after_days: 30
    some_future_field: 999
""",
        )
        config = load_config(p)
        assert config.lifecycle.ttl.archive_after_days == 30


# ---------------------------------------------------------------------------
# MIP loader null guards
# ---------------------------------------------------------------------------


class TestLoaderNullGuards:
    def test_any_null_in_match_block(self, tmp_path: Path) -> None:
        """any: null in YAML should not crash the loader."""
        from astrocyte.mip.loader import load_mip_config

        p = tmp_path / "mip.yaml"
        p.write_text("""
rules:
  - name: test
    priority: 1
    match:
      any: null
    action:
      bank: test-bank
""")
        # Should not raise
        config = load_mip_config(p)
        assert len(config.rules) == 1

    def test_none_null_in_match_block(self, tmp_path: Path) -> None:
        from astrocyte.mip.loader import load_mip_config

        p = tmp_path / "mip.yaml"
        p.write_text("""
rules:
  - name: test
    priority: 1
    match:
      none: null
    action:
      bank: test-bank
""")
        config = load_mip_config(p)
        assert len(config.rules) == 1
