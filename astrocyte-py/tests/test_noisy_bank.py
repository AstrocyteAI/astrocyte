"""Noisy-bank detection (``signal_quality.noisy_bank``, policy-layer.md §3.3)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.errors import RateLimited
from astrocyte.policy.signal_quality import NoisyBankDetector
from astrocyte.testing.in_memory import InMemoryEngineProvider

_THRESHOLDS = {"retain_spike_multiplier": 5.0, "min_avg_content_length": 20, "max_dedup_rate": 0.8}
_LONG = "a perfectly ordinary memory of some length"


class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


def _detector() -> tuple[NoisyBankDetector, _Clock]:
    clock = _Clock()
    return NoisyBankDetector(clock=clock), clock


def _reasons(detector: NoisyBankDetector, bank_id: str = "b1") -> tuple[str, ...]:
    return detector.check(bank_id, **_THRESHOLDS)[0]


# ── Detector ──


def test_unknown_bank_is_not_flagged() -> None:
    detector, _ = _detector()
    assert detector.check("b1", **_THRESHOLDS) == ((), False)


def test_short_content_needs_enough_samples() -> None:
    detector, _ = _detector()
    for _ in range(NoisyBankDetector.MIN_SAMPLES - 1):
        detector.record("b1", 3, False)
    assert _reasons(detector) == ()
    detector.record("b1", 3, False)
    assert _reasons(detector) == ("short_content",)


def test_short_content_looks_only_at_the_recent_window() -> None:
    detector, _ = _detector()
    for _ in range(NoisyBankDetector.SAMPLE_WINDOW):
        detector.record("b1", 3, False)
    for _ in range(NoisyBankDetector.SAMPLE_WINDOW):
        detector.record("b1", len(_LONG), False)
    assert _reasons(detector) == ()


def test_high_dedup_rate() -> None:
    detector, _ = _detector()
    for i in range(NoisyBankDetector.MIN_SAMPLES):
        detector.record("b1", len(_LONG), deduplicated=i % 10 != 0)  # 90% duplicates
    assert _reasons(detector) == ("high_dedup_rate",)


def _steady_history(detector: NoisyBankDetector, clock: _Clock, minutes: int) -> None:
    """One retain a minute for ``minutes`` minutes, ending just over a minute ago."""
    for _ in range(minutes):
        detector.record("b1", len(_LONG), False)
        clock.now += 60
    clock.now += 1


def test_retain_spike_against_the_earlier_rate() -> None:
    detector, clock = _detector()
    _steady_history(detector, clock, minutes=10)
    for _ in range(NoisyBankDetector.MIN_BURST):
        detector.record("b1", len(_LONG), False)
    assert _reasons(detector) == ("retain_spike",)


def test_spike_needs_a_minimum_burst() -> None:
    detector, clock = _detector()
    _steady_history(detector, clock, minutes=10)
    for _ in range(NoisyBankDetector.MIN_BURST - 1):
        detector.record("b1", len(_LONG), False)
    assert _reasons(detector) == ()


def test_spike_needs_baseline_history() -> None:
    """A bank's first burst has nothing to compare with — no flag."""
    detector, _ = _detector()
    for _ in range(NoisyBankDetector.MIN_BURST * 3):
        detector.record("b1", len(_LONG), False)
    assert _reasons(detector) == ()


def test_quiet_bank_baseline_is_floored_at_one_a_minute() -> None:
    """Two retains in ten minutes is a 0.2/min baseline; 12 in the next minute
    is 60x that but only 12x the one-a-minute floor — under a 15x multiplier."""
    detector, clock = _detector()
    detector.record("b1", len(_LONG), False)
    clock.now += 300
    detector.record("b1", len(_LONG), False)
    clock.now += 301
    for _ in range(12):
        detector.record("b1", len(_LONG), False)
    thresholds = {**_THRESHOLDS, "retain_spike_multiplier": 15.0}
    assert detector.check("b1", **thresholds)[0] == ()


def test_steady_high_rate_is_not_a_spike() -> None:
    detector, clock = _detector()
    for _ in range(10):  # 20 a minute for 10 minutes, then the same again
        for _ in range(20):
            detector.record("b1", len(_LONG), False)
        clock.now += 60
    for _ in range(20):
        detector.record("b1", len(_LONG), False)
    assert _reasons(detector) == ()


def test_samples_expire_so_flags_clear() -> None:
    detector, clock = _detector()
    for _ in range(NoisyBankDetector.MIN_SAMPLES):
        detector.record("b1", 3, False)
    assert _reasons(detector) == ("short_content",)
    clock.now += NoisyBankDetector.HORIZON_SECONDS + 1
    assert _reasons(detector) == ()


def test_partial_expiry_keeps_recent_samples() -> None:
    detector, clock = _detector()
    detector.record("b1", 3, False)
    clock.now += NoisyBankDetector.HORIZON_SECONDS + 1
    for _ in range(NoisyBankDetector.MIN_SAMPLES):
        detector.record("b1", 3, False)
    assert _reasons(detector) == ("short_content",)


def test_changed_reports_transitions_only() -> None:
    detector, clock = _detector()
    for _ in range(NoisyBankDetector.MIN_SAMPLES):
        detector.record("b1", 3, False)
    assert detector.check("b1", **_THRESHOLDS) == (("short_content",), True)
    assert detector.check("b1", **_THRESHOLDS) == (("short_content",), False)
    clock.now += NoisyBankDetector.HORIZON_SECONDS + 1
    assert detector.check("b1", **_THRESHOLDS) == ((), True)
    assert detector.check("b1", **_THRESHOLDS) == ((), False)


def test_least_recently_used_bank_is_evicted(monkeypatch) -> None:
    monkeypatch.setattr(NoisyBankDetector, "_MAX_BANKS", 2)
    detector, _ = _detector()
    for _ in range(NoisyBankDetector.MIN_SAMPLES):
        detector.record("a", 3, False)
    assert _reasons(detector, "a") == ("short_content",)
    detector.record("b", 3, False)
    detector.record("a", 3, False)  # a is now most recent
    detector.record("c", 3, False)  # evicts b
    assert set(detector._events) == {"a", "c"}
    detector.record("d", 3, False)  # evicts a, and its flag state
    assert set(detector._events) == {"c", "d"}
    assert "a" not in detector._flagged


# ── Retain integration ──


def _brain(tmp_path: Path, text: str = "") -> Astrocyte:
    path = tmp_path / "astrocyte.yaml"
    path.write_text("provider: test\n" + text)
    brain = Astrocyte.from_config(path)
    brain.set_engine_provider(InMemoryEngineProvider())
    return brain


async def _flag_short_content(brain: Astrocyte, bank_id: str = "b1") -> None:
    for i in range(NoisyBankDetector.MIN_SAMPLES):
        await brain.retain(f"n{i}", bank_id=bank_id)


def _noisy_logs(caplog) -> list[dict]:
    entries = [json.loads(r.getMessage()) for r in caplog.records if r.name == "astrocyte"]
    return [e for e in entries if e["event"].startswith("astrocyte.signal_quality.noisy_bank")]


@pytest.mark.asyncio
async def test_warn_logs_once_and_keeps_storing(tmp_path: Path, caplog, monkeypatch) -> None:
    brain = _brain(tmp_path)
    counted: list[dict] = []
    monkeypatch.setattr(brain._metrics, "inc_counter", lambda name, labels, *a: counted.append({name: labels}))
    with caplog.at_level(logging.INFO, logger="astrocyte"):
        await _flag_short_content(brain)
        assert (await brain.retain("n-extra", bank_id="b1")).stored is True
        assert (await brain.retain("n-extra-2", bank_id="b1")).stored is True
    logs = _noisy_logs(caplog)
    assert len(logs) == 1, "one transition, not one log per retain"
    assert logs[0]["data"] == {"reasons": "short_content", "action": "warn"}
    noisy_counts = [c for c in counted if "astrocyte_noisy_bank_total" in c]
    assert noisy_counts == [{"astrocyte_noisy_bank_total": {"bank_id": "b1", "action": "warn"}}] * 2


@pytest.mark.asyncio
async def test_clearing_is_logged(tmp_path: Path, caplog) -> None:
    brain = _brain(tmp_path)
    await _flag_short_content(brain)
    with caplog.at_level(logging.INFO, logger="astrocyte"):
        await brain.retain("n-extra", bank_id="b1")
        for i in range(NoisyBankDetector.SAMPLE_WINDOW):
            await brain.retain(f"{_LONG} {i}", bank_id="b1")
    events = [e["event"] for e in _noisy_logs(caplog)]
    assert events == ["astrocyte.signal_quality.noisy_bank", "astrocyte.signal_quality.noisy_bank_cleared"]


@pytest.mark.asyncio
async def test_throttle_raises_rate_limited(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "signal_quality:\n  noisy_bank:\n    action: throttle\n")
    await _flag_short_content(brain)
    with pytest.raises(RateLimited) as exc:
        await brain.retain(_LONG, bank_id="b1")
    assert exc.value.retry_after_seconds == 60.0


@pytest.mark.asyncio
async def test_reject_refuses_the_retain(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "signal_quality:\n  noisy_bank:\n    action: reject\n")
    await _flag_short_content(brain)
    result = await brain.retain(_LONG, bank_id="b1")
    assert result.stored is False
    assert result.error == "Bank flagged as noisy (short_content)"


@pytest.mark.asyncio
async def test_disabled_detection_never_flags(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "signal_quality:\n  noisy_bank:\n    enabled: false\n    action: reject\n")
    await _flag_short_content(brain)
    assert (await brain.retain("n-extra", bank_id="b1")).stored is True
    assert brain._policy.check_noisy_bank("b1") is None


@pytest.mark.asyncio
async def test_noisy_bank_settings_are_per_bank(tmp_path: Path) -> None:
    brain = _brain(
        tmp_path,
        "banks:\n  strict:\n    signal_quality:\n      noisy_bank:\n        action: reject\n",
    )
    await _flag_short_content(brain, "strict")
    await _flag_short_content(brain, "lenient")
    assert (await brain.retain(_LONG, bank_id="strict")).stored is False
    assert (await brain.retain(_LONG, bank_id="lenient")).stored is True


@pytest.mark.asyncio
async def test_deduplicated_retains_feed_the_dedup_rate(tmp_path: Path) -> None:
    from astrocyte.types import RetainResult

    brain = _brain(tmp_path, "signal_quality:\n  noisy_bank:\n    action: reject\n")

    async def dedup_everything(request):
        return RetainResult(stored=False, deduplicated=True)

    brain._engine_provider.retain = dedup_everything
    for _ in range(NoisyBankDetector.MIN_SAMPLES):
        await brain.retain(_LONG, bank_id="b1")
    verdict = brain._policy.check_noisy_bank("b1")
    assert verdict is not None and verdict.reasons == ("high_dedup_rate",)
