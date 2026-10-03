"""OpenAIProvider must be able to bound its in-flight request count.

Why this exists. Astrocyte's retain path spawns background consolidation per
call. That backlog is capped (``max_pending_consolidations``), but the cap
bounds *pending tasks*, not concurrent HTTP requests — every admitted task can
be in flight simultaneously, alongside foreground traffic. Against a metered,
rate-limited API that converts an ingest spike into 429s.

This matters specifically for the AML submission: the leaderboard mandates
gpt-4o-mini for Add and Search, and the evaluator applies exactly the sustained
Add load that produces the spike. A grep for "Semaphore" in this provider
returned zero before this change.

The bound is opt-in (0/None = unbounded) because the SDK multiplexes over
HTTP/2 and a tight default would throttle healthy deployments. These tests pin
both directions, so neither the bound nor the opt-out can regress silently.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("openai")

from astrocyte.providers.openai import OpenAIProvider  # noqa: E402
from astrocyte.types import Message  # noqa: E402


class _Recorder:
    """Stands in for the SDK, recording peak overlap of in-flight calls."""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, **kwargs):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        raise RuntimeError("stop-after-dispatch")


def _provider(**kw) -> OpenAIProvider:
    return OpenAIProvider(api_key="sk-test-not-called", **kw)


async def _fire(provider, rec, n: int) -> None:
    provider._client.chat.completions.create = rec

    async def one():
        try:
            await provider.complete([Message(role="user", content="x")])
        except Exception:
            pass  # the recorder always raises; we only measure overlap

    await asyncio.gather(*(one() for _ in range(n)))


class TestBoundIsEnforced:
    async def test_peak_overlap_respects_the_cap(self):
        p = _provider(max_concurrency=3)
        rec = _Recorder()
        await _fire(p, rec, 20)
        # Assert the bound AND that calls actually reached the client — a peak
        # of 0 would satisfy "<= 3" while proving nothing. The first draft of
        # this test passed vacuously for exactly that reason.
        assert rec.peak > 0, "no call reached the client; the test proves nothing"
        assert rec.peak <= 3, f"peak in-flight {rec.peak} exceeded cap 3"

    async def test_all_calls_still_dispatch(self):
        """Bounding must throttle, not drop."""
        p = _provider(max_concurrency=2)
        rec = _Recorder()
        calls = 0

        async def counting(**kw):
            nonlocal calls
            calls += 1
            return await rec(**kw)

        await _fire(p, counting, 12)
        assert calls == 12


class TestOptOutPreserved:
    async def test_unbounded_by_default(self):
        """Default must not throttle existing deployments."""
        p = _provider()
        assert p._sem is None
        rec = _Recorder()
        await _fire(p, rec, 10)
        assert rec.peak > 3, f"expected unbounded fan-out, peaked at {rec.peak}"

    async def test_zero_means_unbounded(self):
        assert _provider(max_concurrency=0)._sem is None

    async def test_env_var_configures_it(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_OPENAI_MAX_CONCURRENCY", "5")
        assert _provider()._sem is not None

    async def test_explicit_arg_beats_env(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_OPENAI_MAX_CONCURRENCY", "5")
        assert _provider(max_concurrency=0)._sem is None
