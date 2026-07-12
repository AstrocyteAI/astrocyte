"""Unit tests for the bench token/cost meter (scripts/_bench_usage.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from astrocyte.types import Completion, Message, TokenUsage  # noqa: E402
from scripts._bench_usage import MeteredProvider, UsageMeter, _price_for  # noqa: E402


class _FakeProvider:
    _model = "gpt-4o-mini"
    _embedding_model = "text-embedding-3-small"

    def __init__(self, with_usage: bool = True) -> None:
        self.with_usage = with_usage

    async def complete(self, messages, **kwargs):
        usage = TokenUsage(input_tokens=100, output_tokens=25) if self.with_usage else None
        return Completion(text="four words of text", model="gpt-4o-mini-2024-07-18", usage=usage)

    async def embed(self, texts, model=None):
        return [[0.0] for _ in texts]

    def capabilities(self):
        return "caps-sentinel"


async def test_complete_counts_api_usage() -> None:
    meter = UsageMeter()
    p = MeteredProvider(_FakeProvider(), meter)
    await p.complete([Message(role="user", content="hi")])
    r = meter.report()
    assert r["total_input_tokens"] == 100
    assert r["total_output_tokens"] == 25
    assert r["total_calls"] == 1
    # Dated model snapshot prices via prefix match.
    assert r["cost_usd"] == pytest.approx((100 * 0.15 + 25 * 0.60) / 1e6, abs=1e-6)
    assert "estimated_calls" not in r["models"]["gpt-4o-mini-2024-07-18"]


async def test_complete_falls_back_to_estimate() -> None:
    meter = UsageMeter()
    p = MeteredProvider(_FakeProvider(with_usage=False), meter)
    await p.complete([Message(role="user", content="hello world")])
    r = meter.report()
    assert r["total_input_tokens"] > 0
    assert r["models"]["gpt-4o-mini-2024-07-18"]["estimated_calls"] == 1


async def test_embed_counts_tokens() -> None:
    meter = UsageMeter()
    p = MeteredProvider(_FakeProvider(), meter)
    await p.embed(["some text to embed", "and another"])
    r = meter.report()
    assert r["models"]["text-embedding-3-small"]["input_tokens"] > 0
    assert r["models"]["text-embedding-3-small"]["calls"] == 1


async def test_phase_split_deltas() -> None:
    meter = UsageMeter()
    p = MeteredProvider(_FakeProvider(), meter)
    await p.complete([Message(role="user", content="a")])
    meter.mark_phase("ingest")
    await p.complete([Message(role="user", content="b")])
    await p.complete([Message(role="user", content="c")])
    meter.mark_phase("query")
    r = meter.report()
    ingest = r["phases"]["ingest"]["models"]["gpt-4o-mini-2024-07-18"]
    query = r["phases"]["query"]["models"]["gpt-4o-mini-2024-07-18"]
    assert ingest["calls"] == 1 and query["calls"] == 2
    assert r["phases"]["ingest"]["cost_usd"] + r["phases"]["query"]["cost_usd"] == pytest.approx(
        r["cost_usd"], abs=1e-6
    )


def test_delegation_and_pricing() -> None:
    meter = UsageMeter()
    p = MeteredProvider(_FakeProvider(), meter)
    assert p.capabilities() == "caps-sentinel"  # __getattr__ passthrough
    assert _price_for("gpt-4o-mini-2024-07-18") == (0.15, 0.60)
    assert _price_for("gpt-4o-2024-08-06") == (2.50, 10.00)  # longest-prefix, not gpt-4o-mini
    assert _price_for("claude-sonnet-5") is None


async def test_unpriced_model_flagged() -> None:
    meter = UsageMeter()
    meter.add("mystery-model", 1000, 100)
    r = meter.report()
    assert r["unpriced_models"] == ["mystery-model"]
    assert r["cost_usd"] == 0.0
