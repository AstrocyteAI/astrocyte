"""MIP Phase 5 first batch — operator ergonomics.

Covers:
- 3a Shadow mode (rule.shadow): rule is evaluated and logged but its action
  is NOT applied; routing proceeds as if the rule didn't match.
- 3e Tie-breaking (config.tie_breaker): "first" (default), "error", and
  "most_specific" policies for resolving same-priority matches.
- 3b Time-bounded activation (rule.active_from / rule.active_until):
  rules outside their window are skipped.
- 3d Per-rule observability tags (rule.observability_tags): surfaced on
  the RoutingDecision for downstream metrics/logging.
"""

from __future__ import annotations

import logging
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from astrocyte.errors import ConfigError, MipRoutingError
from astrocyte.mip.loader import _parse_mip_config, load_mip_config
from astrocyte.mip.router import MipRouter
from astrocyte.mip.rule_engine import RuleEngineInput
from astrocyte.mip.schema import (
    ActionSpec,
    MatchBlock,
    MatchSpec,
    MipConfig,
    RoutingRule,
)


def _rule(
    name: str,
    *,
    priority: int = 10,
    bank: str | None = None,
    match_field: str = "content_type",
    match_value: str = "text",
    extra_match: list[MatchSpec] | None = None,
    shadow: bool = False,
    active_from: datetime | None = None,
    active_until: datetime | None = None,
    observability_tags: list[str] | None = None,
    override: bool = False,
) -> RoutingRule:
    conds = [MatchSpec(field=match_field, operator="eq", value=match_value)]
    if extra_match:
        conds.extend(extra_match)
    return RoutingRule(
        name=name,
        priority=priority,
        match=MatchBlock(all_conditions=conds),
        action=ActionSpec(bank=bank or f"bank-{name}"),
        override=override,
        shadow=shadow,
        active_from=active_from,
        active_until=active_until,
        observability_tags=observability_tags,
    )


def _input(content_type: str = "text", **kw) -> RuleEngineInput:
    return RuleEngineInput(content="x", content_type=content_type, **kw)


# ---------------------------------------------------------------------------
# 3a — Shadow mode
# ---------------------------------------------------------------------------


class TestShadowMode:
    def test_shadow_rule_does_not_route(self, caplog) -> None:
        cfg = MipConfig(rules=[
            _rule("shadow-experiment", priority=5, shadow=True, bank="experimental"),
            _rule("live-default", priority=10, bank="default"),
        ])
        router = MipRouter(cfg)
        with caplog.at_level(logging.INFO, logger="astrocyte.mip"):
            decision = router.route_sync(_input())
        assert decision is not None
        assert decision.bank_id == "default"  # NOT 'experimental'
        assert decision.rule_name == "live-default"
        # Shadow match should still be logged for observability.
        assert any("shadow match" in r.getMessage() for r in caplog.records)

    def test_shadow_override_does_not_lock(self, caplog) -> None:
        """A shadow override is logged but does NOT short-circuit routing."""
        cfg = MipConfig(rules=[
            _rule(
                "shadow-pii", priority=1, shadow=True, override=True,
                bank="quarantine", match_field="pii_detected", match_value=True,
            ),
            _rule("live-default", priority=10, bank="default"),
        ])
        router = MipRouter(cfg)
        with caplog.at_level(logging.INFO, logger="astrocyte.mip"):
            decision = router.route_sync(_input(pii_detected=True))
        assert decision is not None
        assert decision.bank_id == "default"
        assert any("shadow match" in r.getMessage() for r in caplog.records)

    def test_non_matching_shadow_rule_is_silent(self, caplog) -> None:
        cfg = MipConfig(rules=[
            _rule("shadow-x", priority=5, shadow=True, match_value="other"),
            _rule("live", priority=10, bank="default"),
        ])
        router = MipRouter(cfg)
        with caplog.at_level(logging.INFO, logger="astrocyte.mip"):
            router.route_sync(_input())
        assert not any("shadow match" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 3e — Tie-breaking
# ---------------------------------------------------------------------------


class TestTieBreaker:
    def _two_at_same_priority(self, *, with_extra_specificity: bool = False) -> MipConfig:
        # Both rules match content_type=text at priority 10; rule 'wide' has
        # only 1 condition while 'specific' has 2 (used for most_specific).
        wide = _rule("wide", priority=10, bank="wide-bank")
        specific = _rule(
            "specific", priority=10, bank="specific-bank",
            extra_match=([MatchSpec(field="metadata.tier", operator="eq", value="gold")]
                         if with_extra_specificity else None),
        )
        return MipConfig(rules=[wide, specific])

    def test_default_tie_breaker_is_first(self) -> None:
        cfg = self._two_at_same_priority()
        assert cfg.tie_breaker == "first"
        decision = MipRouter(cfg).route_sync(_input())
        assert decision is not None
        assert decision.rule_name == "wide"  # declaration order

    def test_error_tie_breaker_raises(self) -> None:
        cfg = self._two_at_same_priority()
        cfg.tie_breaker = "error"
        with pytest.raises(MipRoutingError, match="tie_breaker=error"):
            MipRouter(cfg).route_sync(_input())

    def test_most_specific_picks_rule_with_more_conditions(self) -> None:
        cfg = self._two_at_same_priority(with_extra_specificity=True)
        cfg.tie_breaker = "most_specific"
        decision = MipRouter(cfg).route_sync(
            _input(metadata={"tier": "gold"}),
        )
        assert decision is not None
        assert decision.rule_name == "specific"

    def test_no_tie_breaker_when_priorities_differ(self) -> None:
        # Both rules match content_type=text but at different priorities,
        # so tie_breaker=error must NOT raise (no priority tie).
        cfg = MipConfig(rules=[
            _rule("a", priority=5, bank="a"),
            _rule("b", priority=10, bank="b"),
        ])
        cfg.tie_breaker = "error"
        # Should not raise (no priority tie). Decision may still be None due
        # to the existing escalation policy when multiple matches exist;
        # the contract under test is only "tie_breaker doesn't fire".
        MipRouter(cfg).route_sync(_input())

    def test_loader_rejects_unknown_tie_breaker(self) -> None:
        with pytest.raises(ConfigError, match="tie_breaker"):
            _parse_mip_config({"version": "1.0", "tie_breaker": "random"})

    def test_loader_accepts_valid_tie_breaker(self) -> None:
        cfg = _parse_mip_config({"version": "1.0", "tie_breaker": "most_specific"})
        assert cfg.tie_breaker == "most_specific"


# ---------------------------------------------------------------------------
# 3b — Time-bounded rule activation
# ---------------------------------------------------------------------------


class TestActivationWindow:
    def test_rule_before_active_from_is_skipped(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=1)
        cfg = MipConfig(rules=[
            _rule("future", priority=5, bank="future", active_from=future),
            _rule("live", priority=10, bank="default"),
        ])
        decision = MipRouter(cfg).route_sync(_input())
        assert decision is not None
        assert decision.rule_name == "live"

    def test_rule_after_active_until_is_skipped(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=1)
        cfg = MipConfig(rules=[
            _rule("expired", priority=5, bank="expired", active_until=past),
            _rule("live", priority=10, bank="default"),
        ])
        decision = MipRouter(cfg).route_sync(_input())
        assert decision is not None
        assert decision.rule_name == "live"

    def test_rule_inside_window_fires(self) -> None:
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        end = datetime.now(timezone.utc) + timedelta(hours=1)
        cfg = MipConfig(rules=[
            _rule("scheduled", priority=5, bank="scheduled",
                  active_from=start, active_until=end),
        ])
        decision = MipRouter(cfg).route_sync(_input())
        assert decision is not None
        assert decision.rule_name == "scheduled"

    def test_loader_parses_iso_strings(self, tmp_path: Path) -> None:
        path = tmp_path / "mip.yaml"
        path.write_text(textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: scheduled
                priority: 10
                active_from: "2030-01-01T00:00:00+00:00"
                active_until: "2030-12-31T23:59:59+00:00"
                match: { content_type: text }
                action: { bank: b }
        """))
        cfg = load_mip_config(path)
        rule = cfg.rules[0]
        assert rule.active_from == datetime(2030, 1, 1, tzinfo=timezone.utc)
        assert rule.active_until == datetime(2030, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    def test_loader_rejects_inverted_window(self) -> None:
        with pytest.raises(ConfigError, match="active_until must be strictly after"):
            _parse_mip_config({
                "version": "1.0",
                "rules": [{
                    "name": "bad", "priority": 10,
                    "active_from": "2030-12-31T00:00:00+00:00",
                    "active_until": "2030-01-01T00:00:00+00:00",
                    "match": {"content_type": "text"},
                    "action": {"bank": "b"},
                }],
            })

    def test_loader_rejects_malformed_datetime(self) -> None:
        with pytest.raises(ConfigError, match="active_from"):
            _parse_mip_config({
                "version": "1.0",
                "rules": [{
                    "name": "bad", "priority": 10,
                    "active_from": "not-a-date",
                    "match": {"content_type": "text"},
                    "action": {"bank": "b"},
                }],
            })


# ---------------------------------------------------------------------------
# 3d — Per-rule observability tags
# ---------------------------------------------------------------------------


class TestObservabilityTags:
    def test_tags_surface_on_routing_decision(self) -> None:
        cfg = MipConfig(rules=[
            _rule("tagged", priority=10, bank="b", observability_tags=["compliance", "pii"]),
        ])
        decision = MipRouter(cfg).route_sync(_input())
        assert decision is not None
        assert decision.observability_tags == ["compliance", "pii"]

    def test_absent_tags_default_to_none(self) -> None:
        cfg = MipConfig(rules=[_rule("plain", priority=10, bank="b")])
        decision = MipRouter(cfg).route_sync(_input())
        assert decision is not None
        assert decision.observability_tags is None

    def test_loader_parses_tags(self, tmp_path: Path) -> None:
        path = tmp_path / "mip.yaml"
        path.write_text(textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: r
                priority: 10
                observability_tags: [experiment, canary]
                match: { content_type: text }
                action: { bank: b }
        """))
        cfg = load_mip_config(path)
        assert cfg.rules[0].observability_tags == ["experiment", "canary"]

    def test_loader_rejects_non_string_tags(self) -> None:
        with pytest.raises(ConfigError, match="observability_tags"):
            _parse_mip_config({
                "version": "1.0",
                "rules": [{
                    "name": "bad", "priority": 10,
                    "observability_tags": ["ok", 123],
                    "match": {"content_type": "text"},
                    "action": {"bank": "b"},
                }],
            })
