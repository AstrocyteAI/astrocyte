"""Per-bank temporal_half_life_days override (Session 1 Item 3).

Half-life is a bank property, not a query property or orchestrator
constant: a hot chat bank has a 7-day half-life; a long-term HR or
knowledge-base bank has 90+. MIP's PipelineSpec is the right home for
this because banks are already configured through MIP rules.

Tests here cover:

- Schema field roundtrips through the loader / preset expansion.
- Orchestrator honors per-bank override over its default.
- Validation rejects non-positive values.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from astrocyte.errors import ConfigError
from astrocyte.mip.loader import _parse_mip_config
from astrocyte.mip.presets import expand_preset
from astrocyte.mip.router import MipRouter
from astrocyte.mip.schema import (
    ActionSpec,
    MatchBlock,
    MatchSpec,
    MipConfig,
    PipelineSpec,
    RoutingRule,
)
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import RecallRequest

# ---------------------------------------------------------------------------
# Schema + loader — half-life flows through YAML parsing
# ---------------------------------------------------------------------------


class TestLoaderSchema:
    def test_loader_accepts_float_half_life(self) -> None:
        cfg = _parse_mip_config({
            "version": "1.0",
            "rules": [{
                "name": "r", "priority": 10,
                "match": {"content_type": "text"},
                "action": {
                    "bank": "b",
                    "pipeline": {"version": 1, "temporal_half_life_days": 90.0},
                },
            }],
        })
        spec = cfg.rules[0].action.pipeline
        assert spec is not None
        assert spec.temporal_half_life_days == 90.0

    def test_loader_accepts_int_half_life(self) -> None:
        """Ints are commonly used in YAML (``temporal_half_life_days: 90``).
        Loader must coerce to float."""
        cfg = _parse_mip_config({
            "version": "1.0",
            "rules": [{
                "name": "r", "priority": 10,
                "match": {"content_type": "text"},
                "action": {
                    "bank": "b",
                    "pipeline": {"version": 1, "temporal_half_life_days": 90},
                },
            }],
        })
        spec = cfg.rules[0].action.pipeline
        assert isinstance(spec.temporal_half_life_days, float)
        assert spec.temporal_half_life_days == 90.0

    def test_loader_rejects_zero(self) -> None:
        with pytest.raises(ConfigError, match="temporal_half_life_days"):
            _parse_mip_config({
                "version": "1.0",
                "rules": [{
                    "name": "r", "priority": 10,
                    "match": {"content_type": "text"},
                    "action": {
                        "bank": "b",
                        "pipeline": {"version": 1, "temporal_half_life_days": 0},
                    },
                }],
            })

    def test_loader_rejects_negative(self) -> None:
        with pytest.raises(ConfigError, match="temporal_half_life_days"):
            _parse_mip_config({
                "version": "1.0",
                "rules": [{
                    "name": "r", "priority": 10,
                    "match": {"content_type": "text"},
                    "action": {
                        "bank": "b",
                        "pipeline": {"version": 1, "temporal_half_life_days": -1},
                    },
                }],
            })

    def test_loader_rejects_non_number(self) -> None:
        with pytest.raises(ConfigError, match="temporal_half_life_days"):
            _parse_mip_config({
                "version": "1.0",
                "rules": [{
                    "name": "r", "priority": 10,
                    "match": {"content_type": "text"},
                    "action": {
                        "bank": "b",
                        "pipeline": {"version": 1, "temporal_half_life_days": "seven"},
                    },
                }],
            })

    def test_loader_absent_field_defaults_to_none(self) -> None:
        """Unset half-life means 'use orchestrator default' — must be
        None so the override logic can detect it."""
        cfg = _parse_mip_config({
            "version": "1.0",
            "rules": [{
                "name": "r", "priority": 10,
                "match": {"content_type": "text"},
                "action": {"bank": "b", "pipeline": {"version": 1}},
            }],
        })
        spec = cfg.rules[0].action.pipeline
        assert spec is not None
        assert spec.temporal_half_life_days is None


# ---------------------------------------------------------------------------
# Preset expansion — explicit override wins over preset default
# ---------------------------------------------------------------------------


class TestPresetExpansion:
    def test_explicit_half_life_survives_preset_expansion(self) -> None:
        """If a rule uses ``preset: code`` AND also sets
        temporal_half_life_days, the explicit value wins."""
        spec = PipelineSpec(
            version=1,
            preset="code",
            temporal_half_life_days=180.0,
        )
        expanded = expand_preset(spec)
        assert expanded.temporal_half_life_days == 180.0
        # And preset's other fields still land.
        assert expanded.chunker is not None
        assert expanded.preset is None  # cleared post-expansion

    def test_absent_override_stays_none_after_expansion(self) -> None:
        """If neither the spec nor the preset sets half-life, it remains
        None — orchestrator then uses its default."""
        spec = PipelineSpec(version=1, preset="code")
        expanded = expand_preset(spec)
        assert expanded.temporal_half_life_days is None


# ---------------------------------------------------------------------------
# Orchestrator integration — override affects retrieval ranking
# ---------------------------------------------------------------------------


def _build_orch_with_rule(half_life_override: float | None) -> PipelineOrchestrator:
    """Build an orchestrator wired to a MIP router whose rule targets
    bank ``b`` with the given half-life override (None = no override)."""
    pipeline_spec = PipelineSpec(
        version=1,
        temporal_half_life_days=half_life_override,
    )
    rule = RoutingRule(
        name="test-rule",
        priority=10,
        match=MatchBlock(all_conditions=[
            MatchSpec(field="content_type", operator="eq", value="text"),
        ]),
        action=ActionSpec(bank="b", pipeline=pipeline_spec),
    )
    mip_config = MipConfig(version="1.0", rules=[rule])

    orch = PipelineOrchestrator(
        vector_store=InMemoryVectorStore(),
        llm_provider=MockLLMProvider(),
        temporal_half_life_days=7.0,   # orchestrator default
    )
    orch.mip_router = MipRouter(mip_config)
    return orch


class TestOrchestratorOverride:
    """The orchestrator must pass the effective half-life (bank override
    vs orchestrator default) through to ``parallel_retrieve``. We assert
    the dispatch directly rather than the downstream score — RRF fusion
    uses ranks, so score differences from half-life changes only surface
    when the change reorders candidates, which requires 3+ items and is
    an indirect test of the intended contract. Dispatch assertions pin
    the mechanism."""

    async def test_override_flows_into_parallel_retrieve(self, monkeypatch) -> None:
        """A 90d bank override must reach parallel_retrieve(), not the
        orchestrator's 7d default."""
        captured: dict[str, float] = {}

        async def fake_parallel_retrieve(**kwargs):
            captured["temporal_half_life_days"] = kwargs["temporal_half_life_days"]
            return {}

        orch = _build_orch_with_rule(half_life_override=90.0)
        monkeypatch.setattr(
            "astrocyte.pipeline.orchestrator.parallel_retrieve",
            fake_parallel_retrieve,
        )
        await orch.recall(RecallRequest(query="q", bank_id="b", max_results=5))
        assert captured["temporal_half_life_days"] == 90.0

    async def test_no_override_uses_orchestrator_default(self, monkeypatch) -> None:
        """When the matched rule's pipeline has no half-life set, the
        orchestrator's constructor value (default 7.0) must be used."""
        captured: dict[str, float] = {}

        async def fake_parallel_retrieve(**kwargs):
            captured["temporal_half_life_days"] = kwargs["temporal_half_life_days"]
            return {}

        orch = _build_orch_with_rule(half_life_override=None)
        monkeypatch.setattr(
            "astrocyte.pipeline.orchestrator.parallel_retrieve",
            fake_parallel_retrieve,
        )
        await orch.recall(RecallRequest(query="q", bank_id="b", max_results=5))
        assert captured["temporal_half_life_days"] == 7.0

    async def test_no_router_uses_orchestrator_default(self, monkeypatch) -> None:
        """When no MIP router is configured at all, the orchestrator
        default still applies — no crashes, no silent None leaking into
        math."""
        captured: dict[str, float] = {}

        async def fake_parallel_retrieve(**kwargs):
            captured["temporal_half_life_days"] = kwargs["temporal_half_life_days"]
            return {}

        orch = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
            temporal_half_life_days=42.0,
        )
        # Deliberately no router: orch.mip_router stays None.
        monkeypatch.setattr(
            "astrocyte.pipeline.orchestrator.parallel_retrieve",
            fake_parallel_retrieve,
        )
        await orch.recall(RecallRequest(query="q", bank_id="b", max_results=5))
        assert captured["temporal_half_life_days"] == 42.0

    async def test_half_life_affects_internal_temporal_score(self) -> None:
        """Half-life changes the raw score _temporal_search emits for a
        given memory — not its rank within the temporal strategy (the
        strategy sorts by age regardless of half-life magnitude), but
        the scalar value the strategy associates with the item.

        This is the narrow, true contract after the Session 1 Item 3
        analysis error. See the session-notes block below the test for
        why this does NOT yet translate to RRF-rank changes, and the
        design fix tracked for a follow-up session.
        """
        from astrocyte.pipeline.retrieval import _temporal_search

        now = datetime.now(timezone.utc)
        store = InMemoryVectorStore()
        # Single 30-day-old memory. Same input, different half-life → different score.
        from astrocyte.types import VectorItem

        await store.store_vectors([
            VectorItem(
                id="v1", bank_id="b", vector=[1.0, 0.0], text="memo",
                metadata={"_created_at": (now - timedelta(days=30)).isoformat()},
            ),
        ])

        short = await _temporal_search(
            store, "b", limit=10, scan_cap=500, half_life_days=7.0,
        )
        long = await _temporal_search(
            store, "b", limit=10, scan_cap=500, half_life_days=90.0,
        )
        # 30 days old:
        # - 7d half-life:  2**(-30/7)  ≈ 0.0506
        # - 90d half-life: 2**(-30/90) ≈ 0.7937
        assert short[0].score == pytest.approx(2.0 ** (-30.0 / 7.0), abs=0.001)
        assert long[0].score == pytest.approx(2.0 ** (-30.0 / 90.0), abs=0.001)
        assert long[0].score > short[0].score


# ---------------------------------------------------------------------------
# Session 1 Item 3 — analysis correction note
# ---------------------------------------------------------------------------
# The original claim was that bumping temporal_half_life_days from 7 to 90
# would lift LongMemEval because older memories would stop being "ignored."
# That's not quite right as implemented:
#
# 1. _temporal_search sorts internally by score (= decay-of-age), which is
#    monotonic in age. The sort order doesn't change with half-life; only
#    the absolute score values do.
# 2. RRF fuses by RANK, not score. So regardless of half-life, the temporal
#    strategy contributes the same per-rank RRF weight to each item.
# 3. Net effect on recall ordering: zero, given the current RRF impl.
#
# The plumbing IS still correct — it's the mechanism that's incomplete.
# Converting temporal from a rank-contributing strategy to a score-aware
# fusion input (e.g. score-weighted RRF variant, or a recency boost
# applied to semantic/keyword scores) is tracked as a follow-up: see the
# todo "Make temporal half-life actually affect fusion ordering" to be
# scheduled after Session 1 measurement.
#
# For Session 1 re-run we continue to set LongMemEval's half-life to 90
# via MIP override — the schema/loader/plumbing delivers the value all
# the way to _temporal_search, and when the follow-up fusion fix lands,
# the config surface is ready. Today's benefit is limited to operators
# who build custom retrieval code that reads the per-bank value.

    def test_math_sanity_of_half_life_constants(self) -> None:
        """Pin the decay formula so regressions on the math are obvious."""
        # age 0 → full weight
        assert math.pow(2.0, -0.0 / 7.0) == 1.0
        # age = half-life → 0.5
        assert math.pow(2.0, -7.0 / 7.0) == pytest.approx(0.5)
        # age = 2 × half-life → 0.25
        assert math.pow(2.0, -14.0 / 7.0) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Documentation invariant — default stays 7 days unless config overrides
# ---------------------------------------------------------------------------


class TestDefaultStable:
    def test_orchestrator_default_stays_7_days(self) -> None:
        """Operators relying on 'a week feels recent' would silently
        shift if this constant changed. Deliberate changes require
        updating this test."""
        orch = PipelineOrchestrator(
            vector_store=InMemoryVectorStore(),
            llm_provider=MockLLMProvider(),
        )
        assert orch.temporal_half_life_days == 7.0
