"""Tests for compute_trend() — M21 Phase B.

Locks in: all 5 Trend states reachable, threshold transitions correct,
timezone-naive timestamps handled, empty evidence → STALE.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from astrocyte.pipeline.trend import Trend, compute_trend

_NOW = datetime(2026, 5, 18, tzinfo=timezone.utc)


def _days_ago(n: int) -> datetime:
    return _NOW - timedelta(days=n)


class TestEmptyAndEdgeCases:
    def test_empty_evidence_returns_stale(self):
        assert compute_trend([], now=_NOW) == Trend.STALE

    def test_naive_datetime_assumed_utc(self):
        naive = datetime(2026, 5, 10)
        assert compute_trend([naive], now=_NOW) == Trend.NEW


class TestStale:
    def test_all_old_evidence_is_stale(self):
        ev = [_days_ago(100), _days_ago(120), _days_ago(180)]
        assert compute_trend(ev, now=_NOW) == Trend.STALE


class TestNew:
    def test_all_recent_evidence_is_new(self):
        ev = [_days_ago(1), _days_ago(5), _days_ago(20)]
        assert compute_trend(ev, now=_NOW) == Trend.NEW

    def test_only_boundary_recent_evidence_is_new(self):
        ev = [_days_ago(29)]
        assert compute_trend(ev, now=_NOW) == Trend.NEW


class TestStrengthening:
    def test_more_recent_than_old_is_strengthening(self):
        # 10 recent, 1 old → ratio = (10/30) / (1/60) = 0.333/0.0167 ≈ 20 → STRENGTHENING
        ev = [_days_ago(d) for d in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)] + [_days_ago(100)]
        assert compute_trend(ev, now=_NOW) == Trend.STRENGTHENING


class TestWeakening:
    def test_mostly_old_evidence_is_weakening(self):
        # 1 recent, 10 old → ratio = (1/30) / (10/60) = 0.0333/0.1667 = 0.2 → WEAKENING
        ev = [_days_ago(15)] + [_days_ago(d) for d in (60, 70, 80, 90, 100, 110, 120, 130, 140, 150)]
        assert compute_trend(ev, now=_NOW) == Trend.WEAKENING


class TestStable:
    def test_balanced_density_is_stable(self):
        # 2 recent (30 day window) + 4 old/middle (60 day window) → equal density
        ev = [_days_ago(5), _days_ago(20), _days_ago(40), _days_ago(50), _days_ago(70), _days_ago(80)]
        assert compute_trend(ev, now=_NOW) == Trend.STABLE


class TestThresholdConfiguration:
    def test_recent_days_override(self):
        ev = [_days_ago(45)]
        # Default recent_days=30 → 45 days ago is NOT recent → STALE
        assert compute_trend(ev, now=_NOW) == Trend.STALE
        # recent_days=60 → 45 days ago IS recent → NEW (no old)
        assert compute_trend(ev, now=_NOW, recent_days=60, old_days=120) == Trend.NEW


class TestComputeObservationTrend:
    """Locks in the metadata → Trend pipeline used at recall + MCP surface."""

    def test_modern_metadata_with_source_timestamps(self):
        import json

        from astrocyte.pipeline.observation import compute_observation_trend

        ts = [
            _days_ago(2).isoformat(),
            _days_ago(5).isoformat(),
            _days_ago(10).isoformat(),
        ]
        metadata = {"_obs_source_timestamps": json.dumps(ts)}
        assert compute_observation_trend(metadata, now=_NOW) == Trend.NEW

    def test_legacy_metadata_fallback_to_updated_at(self):
        from astrocyte.pipeline.observation import compute_observation_trend

        # No _obs_source_timestamps → fall back to _obs_updated_at
        metadata = {"_obs_updated_at": _days_ago(5).isoformat()}
        assert compute_observation_trend(metadata, now=_NOW) == Trend.NEW

    def test_legacy_metadata_with_old_updated_at_is_stale(self):
        from astrocyte.pipeline.observation import compute_observation_trend

        metadata = {"_obs_updated_at": _days_ago(120).isoformat()}
        assert compute_observation_trend(metadata, now=_NOW) == Trend.STALE

    def test_no_metadata_returns_stale(self):
        from astrocyte.pipeline.observation import compute_observation_trend

        assert compute_observation_trend(None, now=_NOW) == Trend.STALE
        assert compute_observation_trend({}, now=_NOW) == Trend.STALE

    def test_malformed_json_safely_falls_back(self):
        from astrocyte.pipeline.observation import compute_observation_trend

        metadata = {
            "_obs_source_timestamps": "not-valid-json",
            "_obs_updated_at": _days_ago(2).isoformat(),
        }
        # Malformed → fallback to updated_at → NEW
        assert compute_observation_trend(metadata, now=_NOW) == Trend.NEW
