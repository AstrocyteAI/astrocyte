"""Failure analysis helpers for benchmark result JSON files."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FailureBucket:
    """A grouped failure bucket with example questions."""

    count: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list)

    def add(self, record: dict[str, Any], *, max_examples: int) -> None:
        self.count += 1
        if len(self.examples) < max_examples:
            self.examples.append(
                {
                    "question": record.get("question"),
                    "expected_answer": record.get("expected_answer"),
                    "category": record.get("category"),
                    "canonical_f1": record.get("canonical_f1"),
                    "_precision": record.get("_precision"),
                    "_reciprocal_rank": record.get("_reciprocal_rank"),
                    "_evidence_id_hit": record.get("_evidence_id_hit"),
                    "reflect_answer_preview": record.get("reflect_answer_preview"),
                }
            )


def load_benchmark_result(path: str | Path) -> dict[str, Any]:
    """Load a serialized benchmark result JSON file."""

    with Path(path).open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("benchmark result must be a JSON object")
    return data


def analyze_failures(
    result: dict[str, Any],
    *,
    max_examples: int = 5,
) -> dict[str, Any]:
    """Group failed per-question records into actionable root-cause buckets."""

    records = [
        record
        for record in result.get("per_question", []) or []
        if isinstance(record, dict) and record.get("correct") is False
    ]
    buckets: dict[str, FailureBucket] = {}
    by_category: dict[str, int] = {}
    for record in records:
        category = str(record.get("category", "unknown"))
        by_category[category] = by_category.get(category, 0) + 1
        for bucket_name in _classify_failure(record):
            bucket = buckets.setdefault(bucket_name, FailureBucket())
            bucket.add(record, max_examples=max_examples)

    ranked = {
        name: {"count": bucket.count, "examples": bucket.examples}
        for name, bucket in sorted(buckets.items(), key=lambda item: item[1].count, reverse=True)
    }
    return {
        "total_failed": len(records),
        "by_category": dict(sorted(by_category.items())),
        "buckets": ranked,
        "recommendations": _recommendations(ranked),
    }


def stable_question_slice(
    result: dict[str, Any],
    *,
    size: int = 200,
    seed: str = "locomo-v1",
) -> list[int]:
    """Return stable per-question indices for quick regression runs."""

    records = result.get("per_question", []) or []
    scored: list[tuple[str, int]] = []
    for idx, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        question = str(record.get("question", ""))
        digest = hashlib.sha256(f"{seed}:{question}".encode("utf-8")).hexdigest()
        scored.append((digest, idx))
    return [idx for _, idx in sorted(scored)[:size]]


def _classify_failure(record: dict[str, Any]) -> list[str]:
    buckets: list[str] = []
    category = str(record.get("category", "unknown"))
    relevant_found = int(record.get("_relevant_found") or 0)
    reciprocal_rank = float(record.get("_reciprocal_rank") or 0.0)
    evidence_hit = bool(record.get("_evidence_id_hit"))

    if evidence_hit or relevant_found > 0:
        if reciprocal_rank == 0.0 or reciprocal_rank < 0.34:
            buckets.append("evidence_present_but_low_rank")
        else:
            buckets.append("evidence_present_synthesis_miss")
    else:
        buckets.append("missing_evidence")

    if category == "temporal":
        buckets.append("temporal_normalization_miss")
    if category == "open-domain":
        buckets.append("open_domain_inference_miss")
    if category == "multi-hop":
        buckets.append("multi_hop_evidence_fusion_miss")
    if _looks_wrong_person(record):
        buckets.append("wrong_person_contamination")
    return buckets


def _looks_wrong_person(record: dict[str, Any]) -> bool:
    question_names = _title_names(str(record.get("question", "")))
    if not question_names:
        return False
    top_hits = record.get("recall_top_hits") or []
    for hit in top_hits[:3]:
        if not isinstance(hit, dict):
            continue
        hit_names = _title_names(str(hit.get("text_preview", "")))
        if hit_names and question_names.isdisjoint(hit_names):
            return True
    return False


def _title_names(text: str) -> set[str]:
    return {
        token.strip(".,?!:;'\"()[]").lower()
        for token in text.split()
        if token[:1].isupper() and token.strip(".,?!:;'\"()[]").isalpha()
    }


def _recommendations(buckets: dict[str, dict[str, Any]]) -> list[str]:
    order = [
        ("evidence_present_but_low_rank", "Improve reranking and context diversity before increasing recall breadth."),
        ("wrong_person_contamination", "Strengthen person/entity filters and wrong-subject penalties."),
        ("temporal_normalization_miss", "Persist normalized temporal facts and query time ranges."),
        ("open_domain_inference_miss", "Compile persona/preference observations ahead of raw facts."),
        ("multi_hop_evidence_fusion_miss", "Add entity-path expansion and path-labeled reflect context."),
        ("missing_evidence", "Inspect retain/chunking/extraction coverage for missing source facts."),
    ]
    return [text for key, text in order if key in buckets]
