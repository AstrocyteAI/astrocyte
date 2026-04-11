"""Bank health analytics — in-memory metric collection and health scoring.

Collects per-bank operation counters and computes composite health scores.
See docs/_design/memory-analytics.md for the full specification.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from astrocyte.types import BankHealth, HealthIssue, QualityDataPoint

# ---------------------------------------------------------------------------
# Per-bank operation counters (in-memory, reset on restart)
# ---------------------------------------------------------------------------


@dataclass
class _BankCounters:
    """Rolling counters for a single bank."""

    retain_count: int = 0
    recall_count: int = 0
    reflect_count: int = 0
    recall_hits: int = 0  # recalls that returned >= 1 result
    dedup_count: int = 0  # retains that were near-duplicates
    reflect_success: int = 0  # reflects that produced non-empty answer
    total_recall_score: float = 0.0  # sum of top-hit scores
    total_content_length: int = 0  # sum of retained content lengths
    created_at: float = field(default_factory=time.monotonic)


class BankMetricsCollector:
    """Collects per-bank operation metrics in memory.

    Thread-safe for single-event-loop async usage (no locks needed).
    Counters reset on process restart — this is operational analytics,
    not durable history.
    """

    def __init__(self) -> None:
        self._banks: dict[str, _BankCounters] = defaultdict(_BankCounters)

    def record_retain(self, bank_id: str, content_length: int, *, deduplicated: bool = False) -> None:
        c = self._banks[bank_id]
        c.retain_count += 1
        c.total_content_length += content_length
        if deduplicated:
            c.dedup_count += 1

    def record_recall(self, bank_id: str, hit_count: int, top_score: float = 0.0) -> None:
        c = self._banks[bank_id]
        c.recall_count += 1
        if hit_count > 0:
            c.recall_hits += 1
        c.total_recall_score += top_score

    def record_reflect(self, bank_id: str, *, success: bool) -> None:
        c = self._banks[bank_id]
        c.reflect_count += 1
        if success:
            c.reflect_success += 1

    def get_counters(self, bank_id: str) -> _BankCounters:
        return self._banks[bank_id]

    def bank_ids(self) -> list[str]:
        return list(self._banks.keys())

    def reset(self, bank_id: str | None = None) -> None:
        if bank_id:
            self._banks.pop(bank_id, None)
        else:
            self._banks.clear()


# ---------------------------------------------------------------------------
# Health scoring
# ---------------------------------------------------------------------------

# Metric weights for composite score (sum to 1.0)
_WEIGHTS = {
    "recall_hit_rate": 0.30,
    "avg_recall_score": 0.20,
    "dedup_rate_inv": 0.15,  # lower dedup = healthier
    "reflect_success_rate": 0.15,
    "avg_content_length_norm": 0.10,
    "recall_to_retain_ratio": 0.10,
}


def compute_bank_health(bank_id: str, counters: _BankCounters, memory_count: int = 0) -> BankHealth:
    """Compute a composite health score for a bank from its counters.

    Returns BankHealth with score 0.0–1.0, status, and actionable issues.
    """
    issues: list[HealthIssue] = []
    metrics: dict[str, float] = {}

    # -- Recall hit rate --
    if counters.recall_count > 0:
        hit_rate = counters.recall_hits / counters.recall_count
    else:
        hit_rate = 0.0
    metrics["recall_hit_rate"] = hit_rate
    if counters.recall_count >= 5 and hit_rate < 0.3:
        issues.append(
            HealthIssue(
                severity="critical",
                code="LOW_RECALL_HIT_RATE",
                message=f"Recall hit rate is {hit_rate:.0%} (< 30%)",
                recommendation="Check that retained content matches expected recall queries. "
                "Review embedding model quality.",
            )
        )
    elif counters.recall_count >= 5 and hit_rate < 0.6:
        issues.append(
            HealthIssue(
                severity="warning",
                code="LOW_RECALL_HIT_RATE",
                message=f"Recall hit rate is {hit_rate:.0%} (< 60%)",
                recommendation="Review retained content relevance and embedding quality.",
            )
        )

    # -- Average recall score --
    if counters.recall_hits > 0:
        avg_score = counters.total_recall_score / counters.recall_hits
    else:
        avg_score = 0.0
    metrics["avg_recall_score"] = avg_score

    # -- Dedup rate --
    if counters.retain_count > 0:
        dedup_rate = counters.dedup_count / counters.retain_count
    else:
        dedup_rate = 0.0
    metrics["dedup_rate"] = dedup_rate
    if counters.retain_count >= 10 and dedup_rate > 0.5:
        issues.append(
            HealthIssue(
                severity="warning",
                code="HIGH_DEDUP_RATE",
                message=f"Dedup rate is {dedup_rate:.0%} (> 50%)",
                recommendation="Agent may be retaining duplicate content. "
                "Check for retain loops or missing dedup at the application layer.",
            )
        )

    # -- Reflect success rate --
    if counters.reflect_count > 0:
        reflect_rate = counters.reflect_success / counters.reflect_count
    else:
        reflect_rate = 1.0  # no reflects = no failures
    metrics["reflect_success_rate"] = reflect_rate
    if counters.reflect_count >= 3 and reflect_rate < 0.5:
        issues.append(
            HealthIssue(
                severity="warning",
                code="LOW_REFLECT_SUCCESS",
                message=f"Reflect success rate is {reflect_rate:.0%} (< 50%)",
                recommendation="Check that the bank has enough relevant content for synthesis.",
            )
        )

    # -- Average content length --
    if counters.retain_count > 0:
        avg_len = counters.total_content_length / counters.retain_count
    else:
        avg_len = 0.0
    metrics["avg_content_length"] = avg_len
    if counters.retain_count >= 5 and avg_len < 20:
        issues.append(
            HealthIssue(
                severity="info",
                code="SHORT_CONTENT",
                message=f"Average content length is {avg_len:.0f} chars (< 20)",
                recommendation="Very short content may not embed well. Consider batching related facts.",
            )
        )

    # -- Recall-to-retain ratio --
    if counters.retain_count > 0:
        rr_ratio = counters.recall_count / counters.retain_count
    else:
        rr_ratio = 0.0
    metrics["recall_to_retain_ratio"] = rr_ratio

    metrics["retain_count"] = float(counters.retain_count)
    metrics["recall_count"] = float(counters.recall_count)
    metrics["reflect_count"] = float(counters.reflect_count)
    metrics["memory_count"] = float(memory_count)

    # -- Composite score --
    # Normalize each metric to 0.0–1.0, then weight
    components = {
        "recall_hit_rate": hit_rate,
        "avg_recall_score": min(avg_score, 1.0),
        "dedup_rate_inv": 1.0 - min(dedup_rate, 1.0),
        "reflect_success_rate": reflect_rate,
        "avg_content_length_norm": min(avg_len / 200.0, 1.0),  # 200 chars = perfect
        "recall_to_retain_ratio": min(rr_ratio / 2.0, 1.0),  # 2:1 ratio = perfect
    }

    # If no operations yet, score is neutral
    total_ops = counters.retain_count + counters.recall_count + counters.reflect_count
    if total_ops == 0:
        score = 0.5
    else:
        score = sum(components[k] * _WEIGHTS[k] for k in _WEIGHTS)

    # Map to status
    if score >= 0.7:
        status: str = "healthy"
    elif score >= 0.4:
        status = "warning"
    else:
        status = "unhealthy"

    return BankHealth(
        bank_id=bank_id,
        score=round(score, 4),
        status=status,  # type: ignore[arg-type]
        issues=issues,
        metrics=metrics,
        assessed_at=datetime.now(timezone.utc),
    )


def counters_to_quality_point(counters: _BankCounters) -> QualityDataPoint:
    """Snapshot current counters as a QualityDataPoint for trend tracking."""
    rc = counters.recall_count
    hit_rate = counters.recall_hits / rc if rc > 0 else 0.0
    avg_score = counters.total_recall_score / counters.recall_hits if counters.recall_hits > 0 else 0.0
    dedup_rate = counters.dedup_count / counters.retain_count if counters.retain_count > 0 else 0.0
    reflect_rate = counters.reflect_success / counters.reflect_count if counters.reflect_count > 0 else 1.0

    return QualityDataPoint(
        date=date.today(),
        retain_count=counters.retain_count,
        recall_count=counters.recall_count,
        recall_hit_rate=round(hit_rate, 4),
        avg_recall_score=round(avg_score, 4),
        dedup_rate=round(dedup_rate, 4),
        reflect_success_rate=round(reflect_rate, 4),
    )
