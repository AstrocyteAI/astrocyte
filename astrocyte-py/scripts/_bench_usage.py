"""Token + cost metering for benchmark runs.

Wraps the bench's ``LLMProvider`` so every completion and embedding call is
counted, then serializes a usage/cost report into the results JSON. This turns
each run into its own cost receipt — accuracy-per-dollar instead of an
envelope estimate — and is the substrate for cost-axis trajectory reporting.

Counting sources:
- Completions: ``Completion.usage`` as reported by the API (exact). Calls
  that return no usage fall back to a tiktoken estimate and are counted in
  ``estimated_calls`` so the report is honest about precision.
- Embeddings: the provider SPI returns vectors only, so tokens are counted
  with tiktoken on the input texts (embeddings bill input tokens only; this
  matches the API's own accounting to within truncation rounding).

Pricing is a static table (USD per 1M tokens) — update alongside provider
price changes. Unknown models contribute tokens but no dollars, and are
listed in ``unpriced_models`` rather than silently costing $0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# USD per 1M tokens: (input, output). Embedding models: (input, 0).
# Prices as of 2026-07 (OpenAI list prices).
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
}


def _price_for(model: str) -> tuple[float, float] | None:
    """Longest-prefix match so dated snapshots (gpt-4o-mini-2024-07-18) price."""
    best: tuple[str, tuple[float, float]] | None = None
    for name, price in PRICES_PER_MTOK.items():
        if model.startswith(name) and (best is None or len(name) > len(best[0])):
            best = (name, price)
    return best[1] if best else None


def _estimate_tokens(text: str) -> int:
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        # ~4 chars/token heuristic when tiktoken is unavailable.
        return max(1, len(text) // 4)


@dataclass
class _ModelCounter:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    estimated_calls: int = 0  # calls counted via tiktoken, not API usage


@dataclass
class UsageMeter:
    """Accumulates per-model token counts, with named phase snapshots."""

    models: dict[str, _ModelCounter] = field(default_factory=dict)
    phases: dict[str, dict[str, Any]] = field(default_factory=dict)
    _last_snapshot: dict[str, tuple[int, int, int]] = field(default_factory=dict)

    def add(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        estimated: bool = False,
    ) -> None:
        c = self.models.setdefault(model, _ModelCounter())
        c.input_tokens += input_tokens
        c.output_tokens += output_tokens
        c.calls += 1
        if estimated:
            c.estimated_calls += 1

    def _totals(self) -> dict[str, tuple[int, int, int]]:
        return {m: (c.input_tokens, c.output_tokens, c.calls) for m, c in self.models.items()}

    def mark_phase(self, name: str) -> None:
        """Record the delta since the previous mark (or start) as ``name``."""
        now = self._totals()
        delta: dict[str, Any] = {}
        for model, (inp, out, calls) in now.items():
            p_inp, p_out, p_calls = self._last_snapshot.get(model, (0, 0, 0))
            d = (inp - p_inp, out - p_out, calls - p_calls)
            if any(d):
                delta[model] = {"input_tokens": d[0], "output_tokens": d[1], "calls": d[2]}
        self.phases[name] = {"models": delta, "cost_usd": _cost(delta)}
        self._last_snapshot = now

    def report(self) -> dict[str, Any]:
        models = {
            m: {
                "input_tokens": c.input_tokens,
                "output_tokens": c.output_tokens,
                "calls": c.calls,
                **({"estimated_calls": c.estimated_calls} if c.estimated_calls else {}),
            }
            for m, c in self.models.items()
        }
        unpriced = sorted(m for m in self.models if _price_for(m) is None)
        return {
            "models": models,
            "phases": self.phases,
            "total_input_tokens": sum(c.input_tokens for c in self.models.values()),
            "total_output_tokens": sum(c.output_tokens for c in self.models.values()),
            "total_calls": sum(c.calls for c in self.models.values()),
            "cost_usd": _cost({m: v for m, v in models.items()}),
            **({"unpriced_models": unpriced} if unpriced else {}),
            "pricing_note": "USD at scripts/_bench_usage.py PRICES_PER_MTOK list prices; embeddings tiktoken-counted",
        }


def _cost(models: dict[str, Any]) -> float:
    total = 0.0
    for model, v in models.items():
        price = _price_for(model)
        if price is None:
            continue
        total += v["input_tokens"] / 1e6 * price[0] + v["output_tokens"] / 1e6 * price[1]
    return round(total, 6)


class MeteredProvider:
    """Transparent LLMProvider wrapper feeding a shared :class:`UsageMeter`.

    Signature-agnostic: ``complete``/``embed`` forward ``*args/**kwargs``
    verbatim, everything else delegates via ``__getattr__`` — so it wraps any
    provider (OpenAI, LiteLLM, a reflect-model sibling) without drift.
    """

    def __init__(self, inner: Any, meter: UsageMeter) -> None:
        self._inner = inner
        self._meter = meter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def complete(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        completion = await self._inner.complete(messages, *args, **kwargs)
        model = getattr(completion, "model", None) or getattr(self._inner, "_model", "unknown")
        usage = getattr(completion, "usage", None)
        if usage is not None:
            self._meter.add(model, usage.input_tokens or 0, usage.output_tokens or 0)
        else:
            inp = sum(_estimate_tokens(getattr(m, "content", "") or "") for m in messages)
            out = _estimate_tokens(getattr(completion, "text", "") or "")
            self._meter.add(model, inp, out, estimated=True)
        return completion

    async def embed(self, texts: list[str], *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model") or getattr(self._inner, "_embedding_model", "text-embedding-3-small")
        self._meter.add(model, sum(_estimate_tokens(t) for t in texts), 0, estimated=True)
        return await self._inner.embed(texts, *args, **kwargs)
