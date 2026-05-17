"""Tests for the skepticism→abstention-floor mapping.

The orchestrator's score-floor abstention used to be a deployment-wide
boolean (``adversarial_defense.abstention_enabled``). That model
forced bench harnesses to fork YAML configs because LoCoMo's
adversarial bucket scores by correctly abstaining, while LME has no
abstention-keyed bucket and benefits from never abstaining.

The fix derives the effective abstention floor from a request's
``Dispositions(skepticism=...)``, matching Hindsight's per-bank
disposition primitive. Same primitive a real Astrocyte user would
use to differentiate "skeptical fact-checker" banks from "answer-
everything" banks within one deployment.

These tests pin down the new mapping so future refactors don't
silently break either bench's accuracy assumption.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.orchestrator import (
    _abstention_floor_for_skepticism,
    _resolve_skepticism_for_abstention,
)
from astrocyte.types import Dispositions


class TestAbstentionFloorForSkepticism:
    """``_abstention_floor_for_skepticism`` linearly scales the base
    floor by skepticism; skepticism=1 is the trapdoor that disables
    abstention entirely."""

    def test_skepticism_1_disables_abstention(self):
        # Floor = 0.0 means the orchestrator's check (which compares
        # ``score < floor``) can never trip — effectively never abstain.
        assert _abstention_floor_for_skepticism(1, base_floor=0.5) == 0.0
        assert _abstention_floor_for_skepticism(1, base_floor=0.2) == 0.0

    def test_skepticism_3_matches_legacy_default(self):
        # Skepticism=3 reproduces the legacy ``abstention_enabled=True``
        # behaviour: effective floor == base floor.
        assert _abstention_floor_for_skepticism(3, base_floor=0.2) == pytest.approx(0.2)
        assert _abstention_floor_for_skepticism(3, base_floor=0.5) == pytest.approx(0.5)

    def test_skepticism_5_aggressive(self):
        # Aggressive end of the dial: 2× the base floor.
        assert _abstention_floor_for_skepticism(5, base_floor=0.2) == pytest.approx(0.4)
        assert _abstention_floor_for_skepticism(5, base_floor=0.5) == pytest.approx(1.0)

    def test_intermediate_skepticisms_interpolate(self):
        # Linear scaling between the documented anchors.
        assert _abstention_floor_for_skepticism(2, base_floor=0.4) == pytest.approx(0.2)
        assert _abstention_floor_for_skepticism(4, base_floor=0.4) == pytest.approx(0.6)

    def test_floor_capped_at_1(self):
        # Score is in [0, 1]; capping the floor at 1.0 prevents the
        # check from becoming a NEVER-answer (which would be a foot-gun).
        assert _abstention_floor_for_skepticism(5, base_floor=0.8) == pytest.approx(1.0)


class TestResolveSkepticismForAbstention:
    """Per-call dispositions take precedence over the deprecated
    ``abstention_enabled`` bool. The bool maps to skepticism=3 (legacy
    default) when True and skepticism=1 (never abstain) when False."""

    def test_dispositions_take_precedence(self):
        # Per-call override beats the deployment fallback in either
        # direction.
        assert _resolve_skepticism_for_abstention(Dispositions(skepticism=1), fallback_enabled=True) == 1
        assert _resolve_skepticism_for_abstention(Dispositions(skepticism=5), fallback_enabled=False) == 5

    def test_fallback_when_no_dispositions(self):
        # Backward compat for callers that haven't migrated to passing
        # dispositions per-call: the legacy YAML toggle still works.
        assert _resolve_skepticism_for_abstention(None, fallback_enabled=True) == 3
        assert _resolve_skepticism_for_abstention(None, fallback_enabled=False) == 1

    def test_dispositions_with_default_skepticism(self):
        # Dispositions(skepticism=3) is the dataclass default and
        # should preserve legacy behaviour even when the YAML bool
        # is False.
        assert _resolve_skepticism_for_abstention(Dispositions(), fallback_enabled=False) == 3
