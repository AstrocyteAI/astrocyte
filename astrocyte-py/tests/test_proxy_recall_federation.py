"""Federated (proxy) recall: concurrency, one deadline, breakers (F0), and
dates plus provenance on every hit (F0b). See docs/_design/federated-sources.md.

Before 2026-10-04 sources ran one after another, before local retrieval, with
a fixed 15 s timeout each, so recall paid the SUM of every remote call; and a
remote row lost its dates and provenance and got an invented 0.5 score.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from astrocyte.config import SourceConfig
from astrocyte.recall import proxy
from astrocyte.recall.proxy import _row_to_hit, gather_proxy_hits_for_bank, reset_proxy_breakers
from astrocyte.types import MemoryHit


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    reset_proxy_breakers()
    monkeypatch.delenv("ASTROCYTE_PROXY_RECALL_DEADLINE_SECONDS", raising=False)
    yield
    reset_proxy_breakers()


def _config(*names: str, **overrides) -> SimpleNamespace:
    return SimpleNamespace(
        sources={
            n: SourceConfig(type="proxy", url=f"https://{n}.example/search", target_bank="b1", **overrides)
            for n in names
        }
    )


def _fake_fetch(monkeypatch, behaviour: dict[str, float | Exception], calls: list | None = None):
    """Replace the HTTP call: a float sleeps that long and returns one hit; an
    exception is raised."""

    async def fetch(sid, src, *, query, bank_id, timeout, metrics=None):
        if calls is not None:
            calls.append((sid, timeout))
        b = behaviour[sid]
        if isinstance(b, Exception):
            raise b
        await asyncio.sleep(b)
        return [MemoryHit(text=f"hit from {sid}", score=0.9, source=f"proxy:{sid}")]

    monkeypatch.setattr(proxy, "fetch_proxy_recall_hits", fetch)


class TestConcurrencyAndDeadline:
    async def test_sources_run_concurrently_not_one_after_another(self, monkeypatch):
        _fake_fetch(monkeypatch, {"a": 0.3, "b": 0.3, "c": 0.3})
        start = time.monotonic()
        hits = await gather_proxy_hits_for_bank(_config("a", "b", "c"), query="q", bank_id="b1")
        elapsed = time.monotonic() - start
        assert len(hits) == 3
        assert elapsed < 0.6, f"three 0.3 s sources took {elapsed:.2f} s: they ran in series"

    async def test_a_slow_source_costs_at_most_the_deadline(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_PROXY_RECALL_DEADLINE_SECONDS", "0.3")
        _fake_fetch(monkeypatch, {"fast": 0.01, "slow": 30.0})
        start = time.monotonic()
        hits = await gather_proxy_hits_for_bank(_config("fast", "slow"), query="q", bank_id="b1")
        assert time.monotonic() - start < 1.0
        assert [h.text for h in hits] == ["hit from fast"], "partial results, slow source dropped"

    async def test_hits_come_back_in_config_order_not_completion_order(self, monkeypatch):
        _fake_fetch(monkeypatch, {"first": 0.2, "second": 0.0})
        hits = await gather_proxy_hits_for_bank(_config("first", "second"), query="q", bank_id="b1")
        assert [h.text for h in hits] == ["hit from first", "hit from second"]

    async def test_a_failing_source_does_not_affect_the_others(self, monkeypatch):
        _fake_fetch(monkeypatch, {"ok": 0.0, "broken": RuntimeError("500")})
        hits = await gather_proxy_hits_for_bank(_config("ok", "broken"), query="q", bank_id="b1")
        assert [h.text for h in hits] == ["hit from ok"]

    async def test_per_source_timeout_is_capped_by_the_deadline(self, monkeypatch):
        calls: list = []
        _fake_fetch(monkeypatch, {"a": 0.0}, calls)
        await gather_proxy_hits_for_bank(_config("a", recall_timeout_seconds=5.0), query="q", bank_id="b1")
        assert calls == [("a", proxy._DEFAULT_DEADLINE)]
        calls.clear()
        await gather_proxy_hits_for_bank(_config("a", recall_timeout_seconds=0.2), query="q", bank_id="b1")
        assert calls == [("a", 0.2)]


class TestBreaker:
    async def test_a_source_that_keeps_failing_is_skipped_for_a_cooldown(self, monkeypatch):
        calls: list = []
        _fake_fetch(monkeypatch, {"flaky": RuntimeError("down")}, calls)
        for _ in range(proxy._BREAKER_THRESHOLD):
            await gather_proxy_hits_for_bank(_config("flaky"), query="q", bank_id="b1")
        assert len(calls) == proxy._BREAKER_THRESHOLD
        await gather_proxy_hits_for_bank(_config("flaky"), query="q", bank_id="b1")
        assert len(calls) == proxy._BREAKER_THRESHOLD, "tripped source must not be called"

        real = time.monotonic
        monkeypatch.setattr(proxy.time, "monotonic", lambda: real() + proxy._BREAKER_COOLDOWN_S + 1)
        await gather_proxy_hits_for_bank(_config("flaky"), query="q", bank_id="b1")
        assert len(calls) == proxy._BREAKER_THRESHOLD + 1, "retried after the cool-down"

    async def test_a_success_resets_the_failure_count(self, monkeypatch):
        behaviour: dict = {"s": RuntimeError("blip")}
        calls: list = []
        _fake_fetch(monkeypatch, behaviour, calls)
        for _ in range(proxy._BREAKER_THRESHOLD - 1):
            await gather_proxy_hits_for_bank(_config("s"), query="q", bank_id="b1")
        behaviour["s"] = 0.0
        await gather_proxy_hits_for_bank(_config("s"), query="q", bank_id="b1")
        behaviour["s"] = RuntimeError("blip")
        await gather_proxy_hits_for_bank(_config("s"), query="q", bank_id="b1")
        await gather_proxy_hits_for_bank(_config("s"), query="q", bank_id="b1")
        assert len(calls) == proxy._BREAKER_THRESHOLD + 2, "count restarted after the success"


class TestRowMetadataParity:
    def test_dates_and_provenance_survive(self):
        hit = _row_to_hit(
            "wiki",
            {
                "text": "Deploys happen on Tuesdays.",
                "score": 0.8,
                "occurred_at": "2026-09-01T10:00:00Z",
                "updated_at": "2026-09-15T08:30:00+00:00",
                "url": "https://wiki.example/deploys",
                "etag": 'W/"abc123"',
                "author": "ops-team",
                "anchor": "deploys#schedule",
            },
        )
        assert hit.occurred_at == datetime(2026, 9, 1, 10, tzinfo=UTC)
        assert hit.retained_at == datetime(2026, 9, 15, 8, 30, tzinfo=UTC)
        assert hit.metadata == {
            "_source_url": "https://wiki.example/deploys",
            "_source_version": 'W/"abc123"',
            "_source_author": "ops-team",
            "_source_anchor": "deploys#schedule",
        }
        assert hit.score == 0.8
        assert hit.source == "proxy:wiki"

    def test_a_missing_score_is_flagged_not_passed_off_as_a_measurement(self):
        hit = _row_to_hit("s", {"text": "x"})
        assert hit.metadata == {"_score_missing": True}

    def test_naive_and_epoch_dates_become_utc(self):
        assert _row_to_hit("s", {"text": "x", "occurred_at": "2026-01-02T03:04:05"}).occurred_at == datetime(
            2026, 1, 2, 3, 4, 5, tzinfo=UTC
        )
        assert _row_to_hit("s", {"text": "x", "occurred_at": 0}).occurred_at == datetime(1970, 1, 1, tzinfo=UTC)

    def test_unparseable_dates_are_dropped_not_guessed(self):
        hit = _row_to_hit("s", {"text": "x", "score": 1, "occurred_at": "last Tuesday", "updated_at": True})
        assert hit.occurred_at is None and hit.retained_at is None

    def test_a_source_cannot_spoof_reserved_keys_through_its_metadata(self):
        hit = _row_to_hit(
            "s",
            {
                "text": "x",
                "score": 0.5,
                "url": "https://real.example",
                "metadata": {"_source_url": "https://spoof.example", "_score_missing": True, "team": "a"},
            },
        )
        assert hit.metadata == {"_source_url": "https://real.example", "team": "a"}

    def test_dates_reach_fusion(self):
        """End to end into the fusion input: the dates must not stop at MemoryHit."""
        from astrocyte.pipeline.fusion import memory_hits_as_scored

        hit = _row_to_hit("s", {"text": "x", "score": 0.5, "occurred_at": "2026-09-01T00:00:00Z"})
        assert memory_hits_as_scored([hit])[0].occurred_at == datetime(2026, 9, 1, tzinfo=UTC)


class _Metrics:
    def __init__(self):
        self.observed: list[tuple[str, float, dict]] = []
        self.counted: list[dict] = []

    def inc_counter(self, name, labels, description=""):
        self.counted.append(labels)

    def observe_histogram(self, name, value, labels, description=""):
        self.observed.append((name, value, labels))


class TestLatencyCoversEveryOutcome:
    async def test_a_deadline_miss_is_timed_at_the_deadline(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_PROXY_RECALL_DEADLINE_SECONDS", "0.2")
        _fake_fetch(monkeypatch, {"slow": 5.0})
        m = _Metrics()
        await gather_proxy_hits_for_bank(_config("slow"), query="q", bank_id="b1", metrics=m)
        assert ("astrocyte_proxy_recall_duration_seconds", 0.2, {"source_id": "slow"}) in m.observed
        assert {"source_id": "slow", "status": "timeout"} in m.counted

    async def test_an_error_is_timed_too(self, monkeypatch):
        """The HTTP layer itself: a failed request is observed, not only counted."""

        async def failing_headers(*_a, **_k):
            await asyncio.sleep(0.05)
            raise RuntimeError("auth backend down")

        monkeypatch.setattr(proxy, "build_proxy_headers", failing_headers)
        m = _Metrics()
        with pytest.raises(RuntimeError):
            await proxy.fetch_proxy_recall_hits(
                "s",
                SourceConfig(type="proxy", url="https://s.example/q", target_bank="b1"),
                query="q",
                bank_id="b1",
                metrics=m,
            )
        assert m.counted == [{"source_id": "s", "status": "error"}]
        ((name, value, labels),) = m.observed
        assert labels == {"source_id": "s"} and value >= 0.05


class TestLateAnswers:
    async def test_a_late_answer_serves_the_next_recall_of_the_same_query(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_PROXY_RECALL_DEADLINE_SECONDS", "0.1")
        calls: list = []
        _fake_fetch(monkeypatch, {"slow": 0.25}, calls)
        first = await gather_proxy_hits_for_bank(_config("slow"), query="q", bank_id="b1")
        assert first == []
        await asyncio.sleep(0.3)  # the late fetch finishes in the background
        second = await gather_proxy_hits_for_bank(_config("slow"), query="q", bank_id="b1")
        assert [h.text for h in second] == ["hit from slow"]
        assert len(calls) == 1, "served from the late answer, no new fetch"

    async def test_a_late_answer_is_used_once(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_PROXY_RECALL_DEADLINE_SECONDS", "0.1")
        calls: list = []
        _fake_fetch(monkeypatch, {"slow": 0.15}, calls)
        await gather_proxy_hits_for_bank(_config("slow"), query="q", bank_id="b1")
        await asyncio.sleep(0.2)
        await gather_proxy_hits_for_bank(_config("slow"), query="q", bank_id="b1")
        await gather_proxy_hits_for_bank(_config("slow"), query="q", bank_id="b1")
        assert len(calls) == 2, "the third recall fetches again"

    async def test_a_different_query_or_bank_is_not_served_the_late_answer(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_PROXY_RECALL_DEADLINE_SECONDS", "0.1")
        calls: list = []
        _fake_fetch(monkeypatch, {"slow": 0.15}, calls)
        await gather_proxy_hits_for_bank(_config("slow"), query="q1", bank_id="b1")
        await asyncio.sleep(0.2)
        await gather_proxy_hits_for_bank(_config("slow"), query="q2", bank_id="b1")
        assert len(calls) == 2

    async def test_a_source_that_is_always_late_still_trips_the_breaker(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_PROXY_RECALL_DEADLINE_SECONDS", "0.05")
        calls: list = []
        _fake_fetch(monkeypatch, {"slow": 0.08}, calls)
        for i in range(proxy._BREAKER_THRESHOLD):
            await gather_proxy_hits_for_bank(_config("slow"), query=f"q{i}", bank_id="b1")
            await asyncio.sleep(0.1)  # each late answer completes (and must not reset the count)
        await gather_proxy_hits_for_bank(_config("slow"), query="fresh", bank_id="b1")
        assert len(calls) == proxy._BREAKER_THRESHOLD, "tripped: late successes do not reset it"

    async def test_a_runaway_late_fetch_is_cancelled_at_the_hard_cap(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_PROXY_RECALL_DEADLINE_SECONDS", "0.02")
        _fake_fetch(monkeypatch, {"stuck": 60.0})
        await gather_proxy_hits_for_bank(_config("stuck"), query="q", bank_id="b1")
        assert len(proxy._late_tasks) == 1
        await asyncio.sleep(0.02 * proxy._LATE_HARD_CAP_FACTOR + 0.1)
        assert not proxy._late_tasks, "cancelled after ten deadlines"
