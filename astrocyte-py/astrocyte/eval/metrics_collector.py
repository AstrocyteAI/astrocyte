"""Aggregator for tier-3 benchmark observability.

Hooks installed across the pipeline (orchestrator, entity resolver, eval
harness) push records into a single :class:`BenchmarkMetricsCollector`
instance for the duration of a benchmark run. At the end, the collector
distils the raw signal into the optional fields of :class:`EvalMetrics`
so the result JSON carries:

- Cost (tokens by phase, $USD via per-model pricing)
- Latency tail (p99, end-to-end per question)
- Robustness (errors by type, OpenAI retries, failed retains)
- Quality (recall-coverage, recall↔reflect gap, adversarial abstention)
- Memory architecture (cascade decisions, entity resolved/created,
  composite-score histogram)

Design notes
------------
- The collector is **opt-in** — pipelines look it up via
  ``getattr(brain, "_metrics_collector", None)`` and silently no-op when
  it's ``None``. Production code paths and tests that don't care about
  benchmark metrics stay unaffected.
- Recording methods are cheap (dict update / list append). Heavy lifting
  is done in :meth:`finalize`.
- Per-question records carry enough data for post-hoc analysis without
  pinning whole memory hits in memory — IDs and small floats only.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.types import EvalMetrics

_logger = logging.getLogger("astrocyte.eval.metrics")


# ---------------------------------------------------------------------------
# Pricing — OpenAI per-million-token rates (USD)
# ---------------------------------------------------------------------------
# Updated against OpenAI's published prices. Cost values in
# ``EvalMetrics.cost_total_usd`` are estimates; treat as a relative signal
# rather than a billable number. Easy to update — keys are model names as
# returned in ``Completion.model`` or ``embedding.model``.
_MODEL_PRICING_USD_PER_M_TOKENS: dict[str, tuple[float, float]] = {
    # (input, output) per 1M tokens
    "gpt-4o-mini":              (0.15, 0.60),
    "gpt-4o-mini-2024-07-18":   (0.15, 0.60),
    "gpt-4o":                   (2.50, 10.00),
    "gpt-4o-2024-08-06":        (2.50, 10.00),
    "gpt-4.1":                  (2.00, 8.00),
    "gpt-4.1-mini":             (0.40, 1.60),
    "o1-mini":                  (3.00, 12.00),
    "o3-mini":                  (1.10, 4.40),
    # Embeddings — price per 1M tokens for input only
    "text-embedding-3-small":   (0.02, 0.0),
    "text-embedding-3-large":   (0.13, 0.0),
    "text-embedding-ada-002":   (0.10, 0.0),
}


def _bucket_label(score: float, *, n_buckets: int = 10) -> str:
    """Return histogram bucket label for a ``[0,1]`` score."""
    if not 0.0 <= score <= 1.0:
        score = max(0.0, min(1.0, score))
    idx = min(int(score * n_buckets), n_buckets - 1)
    lo = idx / n_buckets
    hi = (idx + 1) / n_buckets
    return f"{lo:.1f}-{hi:.1f}"


def _percentile(values: list[float], pct: float) -> float | None:
    """Sample percentile (linear interpolation). ``None`` for empty list."""
    if not values:
        return None
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (len(sorted_vals) - 1) * (pct / 100.0)
    lower_idx = int(rank)
    upper_idx = min(lower_idx + 1, len(sorted_vals) - 1)
    weight = rank - lower_idx
    return sorted_vals[lower_idx] * (1.0 - weight) + sorted_vals[upper_idx] * weight


# ---------------------------------------------------------------------------
# Per-question record (compact)
# ---------------------------------------------------------------------------


@dataclass
class _QuestionRecord:
    """Per-question signal captured during eval. Compact by design."""
    idx: int
    category: str | None = None
    correct: bool = False
    recall_hit: bool = False         # gold answer text-matched any retrieved memory
    reflect_hit: bool = False        # gold answer text-matched the synthesised reply
    abstained: bool = False          # reflect refused to answer (adversarial path)
    recall_latency_ms: float | None = None
    reflect_latency_ms: float | None = None
    api_calls: int = 0
    tokens: int = 0


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkMetricsCollector:
    """Aggregates tier-3 metrics during a benchmark run.

    Lives on ``brain._metrics_collector`` for the duration of a bench
    invocation. Pipeline code paths probe with ``getattr`` and silently
    no-op when not present.

    Phase tracking: callers use :meth:`set_phase` to mark transitions
    between ``"retain"``, ``"persona_compile"``, ``"eval"``, etc.
    Subsequent token / API-call records are bucketed under that phase.
    """

    # Mutable phase pointer
    _current_phase: str = "init"

    # Cost / API-call tracking
    _tokens_by_phase: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _api_calls_by_endpoint: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _cost_by_phase: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    # Latency tracking
    _retain_latencies_ms: list[float] = field(default_factory=list)
    _recall_latencies_ms: list[float] = field(default_factory=list)
    _reflect_latencies_ms: list[float] = field(default_factory=list)

    # Per-question records (one per benchmark question)
    _question_records: list[_QuestionRecord] = field(default_factory=list)

    # Robustness
    _error_count_by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _openai_retry_count: int = 0
    _failed_retain_count: int = 0

    # Memory-architecture observability
    _cascade_decisions: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _entities_resolved_count: int = 0   # Path B: rewrote tentative ID → canonical
    _entities_created_count: int = 0    # No match: kept tentative ID as new canonical
    _composite_scores: list[float] = field(default_factory=list)

    # Memory state at finalize-time
    _wiki_pages_total: int | None = None
    _wiki_pages_per_persona: float | None = None

    # ------------------------------------------------------------------
    # Phase / context helpers
    # ------------------------------------------------------------------

    def set_phase(self, phase: str) -> None:
        """Mark the current pipeline phase. All subsequent token / call
        records are bucketed under this phase until the next ``set_phase``."""
        self._current_phase = phase

    @property
    def current_phase(self) -> str:
        return self._current_phase

    # ------------------------------------------------------------------
    # Cost / API-call recording
    # ------------------------------------------------------------------

    def record_completion_call(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Record a chat-completion call: bumps endpoint count, tokens, cost."""
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
        self._tokens_by_phase[self._current_phase] += total_tokens
        self._api_calls_by_endpoint["chat/completions"] += 1
        cost = _estimate_cost_usd(model, input_tokens, output_tokens)
        if cost is not None:
            self._cost_by_phase[self._current_phase] += cost

    def record_embedding_call(self, *, model: str, tokens: int) -> None:
        """Record an embedding call. Embeddings have no output tokens."""
        self._tokens_by_phase[self._current_phase] += int(tokens or 0)
        self._api_calls_by_endpoint["embeddings"] += 1
        cost = _estimate_cost_usd(model, tokens, 0)
        if cost is not None:
            self._cost_by_phase[self._current_phase] += cost

    # ------------------------------------------------------------------
    # Latency recording
    # ------------------------------------------------------------------

    def record_retain_latency(self, ms: float) -> None:
        self._retain_latencies_ms.append(float(ms))

    def record_recall_latency(self, ms: float) -> None:
        self._recall_latencies_ms.append(float(ms))

    def record_reflect_latency(self, ms: float) -> None:
        self._reflect_latencies_ms.append(float(ms))

    # ------------------------------------------------------------------
    # Per-question records
    # ------------------------------------------------------------------

    def record_question(
        self,
        idx: int,
        *,
        category: str | None = None,
        correct: bool = False,
        recall_hit: bool = False,
        reflect_hit: bool = False,
        abstained: bool = False,
        recall_latency_ms: float | None = None,
        reflect_latency_ms: float | None = None,
        api_calls: int = 0,
        tokens: int = 0,
    ) -> None:
        self._question_records.append(_QuestionRecord(
            idx=idx,
            category=category,
            correct=correct,
            recall_hit=recall_hit,
            reflect_hit=reflect_hit,
            abstained=abstained,
            recall_latency_ms=recall_latency_ms,
            reflect_latency_ms=reflect_latency_ms,
            api_calls=api_calls,
            tokens=tokens,
        ))

    # ------------------------------------------------------------------
    # Robustness
    # ------------------------------------------------------------------

    def record_error(self, error_type: str) -> None:
        """Bump the count for *error_type*. Common values: ``"pool_timeout"``,
        ``"deadlock"``, ``"openai_429"``, ``"openai_5xx"``, ``"other"``."""
        self._error_count_by_type[error_type] += 1

    def record_openai_retry(self) -> None:
        self._openai_retry_count += 1

    def record_failed_retain(self) -> None:
        self._failed_retain_count += 1

    # ------------------------------------------------------------------
    # Memory-architecture observability
    # ------------------------------------------------------------------

    def record_cascade_decision(
        self,
        decision: str,
        *,
        composite_score: float | None = None,
    ) -> None:
        """Record one cascade decision. ``decision`` should be one of
        ``"trigram_autolink"``, ``"embedding_autolink"``,
        ``"composite_autolink"``, ``"llm_disambiguation"``, ``"skipped"``,
        ``"no_match"`` (reserved for canonical-resolution path)."""
        self._cascade_decisions[decision] += 1
        if composite_score is not None:
            self._composite_scores.append(float(composite_score))

    def record_canonical_resolution(self, *, resolved: bool) -> None:
        """Path B path: ``True`` when a new entity was rewritten to an
        existing canonical's ID; ``False`` when it stayed novel."""
        if resolved:
            self._entities_resolved_count += 1
        else:
            self._entities_created_count += 1

    # ------------------------------------------------------------------
    # End-of-run state
    # ------------------------------------------------------------------

    def set_wiki_page_stats(self, *, total: int, unique_personas: int) -> None:
        """Snapshot end-of-run wiki-page counts for ratio computation."""
        self._wiki_pages_total = int(total)
        if unique_personas > 0:
            self._wiki_pages_per_persona = total / unique_personas
        else:
            self._wiki_pages_per_persona = 0.0

    # ------------------------------------------------------------------
    # Finalization — produce summary metrics
    # ------------------------------------------------------------------

    def finalize(self, base_metrics: EvalMetrics) -> EvalMetrics:
        """Decorate *base_metrics* with the tier-3 aggregations.

        Returns a NEW EvalMetrics instance with all collected fields
        populated. Idempotent — safe to call multiple times.
        """
        from dataclasses import replace

        # Cost / API
        cost_total = sum(self._cost_by_phase.values())
        n_questions = len(self._question_records) or None
        api_total = sum(self._api_calls_by_endpoint.values())

        # Latency tail
        retain_p99 = _percentile(self._retain_latencies_ms, 99)
        recall_p99 = _percentile(self._recall_latencies_ms, 99)
        reflect_p50 = _percentile(self._reflect_latencies_ms, 50)
        reflect_p95 = _percentile(self._reflect_latencies_ms, 95)
        reflect_p99 = _percentile(self._reflect_latencies_ms, 99)

        # End-to-end per-question (sum of recall + reflect, joined by index)
        e2e_latencies = [
            (q.recall_latency_ms or 0) + (q.reflect_latency_ms or 0)
            for q in self._question_records
            if q.recall_latency_ms is not None or q.reflect_latency_ms is not None
        ]
        e2e_p50 = _percentile(e2e_latencies, 50)
        e2e_p95 = _percentile(e2e_latencies, 95)

        # Recall coverage + cross-tab
        if self._question_records:
            both_hit = sum(1 for q in self._question_records if q.recall_hit and q.reflect_hit)
            recall_only = sum(1 for q in self._question_records if q.recall_hit and not q.reflect_hit)
            reflect_only = sum(1 for q in self._question_records if not q.recall_hit and q.reflect_hit)
            both_miss = sum(1 for q in self._question_records if not q.recall_hit and not q.reflect_hit)
            recall_reflect_gap = {
                "both_hit": both_hit,
                "recall_hit_reflect_miss": recall_only,
                "recall_miss_reflect_hit": reflect_only,
                "both_miss": both_miss,
            }
            recall_coverage = (both_hit + recall_only) / len(self._question_records)
        else:
            recall_reflect_gap = None
            recall_coverage = None

        # Adversarial abstention
        adversarial = [q for q in self._question_records if (q.category or "").lower() == "adversarial"]
        if adversarial:
            abstention_rate_adv = sum(1 for q in adversarial if q.abstained) / len(adversarial)
        else:
            abstention_rate_adv = None

        # Composite-score histogram
        composite_hist: dict[str, int] | None = None
        if self._composite_scores:
            composite_hist = dict(sorted(
                {k: 0 for k in (_bucket_label(i / 10) for i in range(10))}.items()
            ))
            for score in self._composite_scores:
                composite_hist[_bucket_label(score)] = composite_hist.get(_bucket_label(score), 0) + 1

        return replace(
            base_metrics,
            # Quality
            recall_coverage=recall_coverage,
            recall_reflect_gap=recall_reflect_gap,
            abstention_rate_adversarial=abstention_rate_adv,
            # Cost
            tokens_by_phase=dict(self._tokens_by_phase) if self._tokens_by_phase else None,
            api_calls_total=api_total or None,
            api_calls_by_endpoint=dict(self._api_calls_by_endpoint) if self._api_calls_by_endpoint else None,
            cost_total_usd=round(cost_total, 4) if cost_total > 0 else None,
            cost_per_question_usd=round(cost_total / n_questions, 6) if (cost_total > 0 and n_questions) else None,
            # Latency tail
            retain_latency_p99_ms=retain_p99,
            recall_latency_p99_ms=recall_p99,
            reflect_latency_p50_ms=reflect_p50 if base_metrics.reflect_latency_p50_ms is None else base_metrics.reflect_latency_p50_ms,
            reflect_latency_p95_ms=reflect_p95 if base_metrics.reflect_latency_p95_ms is None else base_metrics.reflect_latency_p95_ms,
            reflect_latency_p99_ms=reflect_p99,
            e2e_per_question_p50_ms=e2e_p50,
            e2e_per_question_p95_ms=e2e_p95,
            # Robustness
            error_count_by_type=dict(self._error_count_by_type) if self._error_count_by_type else None,
            openai_retry_count=self._openai_retry_count if self._openai_retry_count else None,
            failed_retain_count=self._failed_retain_count if self._failed_retain_count else None,
            # Memory architecture
            cascade_decisions=dict(self._cascade_decisions) if self._cascade_decisions else None,
            entities_resolved_count=self._entities_resolved_count if self._entities_resolved_count else None,
            entities_created_count=self._entities_created_count if self._entities_created_count else None,
            composite_score_distribution=composite_hist,
            wiki_pages_total=self._wiki_pages_total,
            wiki_pages_per_persona=(
                round(self._wiki_pages_per_persona, 2)
                if self._wiki_pages_per_persona is not None else None
            ),
        )


def _estimate_cost_usd(
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    """Return USD cost estimate for a single API call, or ``None`` when the
    model isn't in the pricing table. Treat results as relative signal."""
    if not model:
        return None
    # Strip API-version suffixes for the lookup
    key = model.split(":")[0]
    pricing = _MODEL_PRICING_USD_PER_M_TOKENS.get(key)
    if pricing is None:
        return None
    in_rate, out_rate = pricing
    cost = (
        (input_tokens or 0) * in_rate / 1_000_000.0
        + (output_tokens or 0) * out_rate / 1_000_000.0
    )
    return cost
