"""MIP rule engine — match DSL evaluator.

All functions are sync and pure (no I/O). Rust migration candidates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from astrocyte.mip.schema import MatchBlock, MatchSpec, RoutingRule
from astrocyte.types import Metadata


@dataclass
class RuleEngineInput:
    """All data available for rule matching."""

    content: str
    content_type: str | None = None
    metadata: Metadata | None = None
    tags: list[str] | None = None
    pii_detected: bool = False
    source: str | None = None
    signals: dict[str, float] | None = None  # Computed signals (word_count, novelty_score)


@dataclass
class RuleMatch:
    """A rule that matched the input."""

    rule: RoutingRule
    confidence: float


_TEMPLATE_PATTERN = re.compile(r"\{([^}]+)\}")


def evaluate_rules(rules: list[RoutingRule], input_data: RuleEngineInput) -> list[RuleMatch]:
    """Evaluate all rules against input. Returns matched rules sorted by priority.

    Override rules (override=True) are checked first and short-circuit.
    """
    # Check override rules first
    for rule in sorted(rules, key=lambda r: r.priority):
        if rule.override and evaluate_match_block(rule.match, input_data):
            return [RuleMatch(rule=rule, confidence=rule.action.confidence)]

    # Check normal rules
    matches: list[RuleMatch] = []
    for rule in sorted(rules, key=lambda r: r.priority):
        if rule.override:
            continue
        if evaluate_match_block(rule.match, input_data):
            matches.append(RuleMatch(rule=rule, confidence=rule.action.confidence))

    return matches


def evaluate_match_block(block: MatchBlock, input_data: RuleEngineInput) -> bool:
    """Evaluate a MatchBlock (all/any/none composition)."""
    # Empty block with no conditions matches everything
    has_conditions = False

    if block.all_conditions is not None:
        has_conditions = True
        # Empty all_conditions list matches everything (fallback rule)
        if block.all_conditions and not all(evaluate_match_spec(s, input_data) for s in block.all_conditions):
            return False

    if block.any_conditions is not None:
        has_conditions = True
        if not any(evaluate_match_spec(s, input_data) for s in block.any_conditions):
            return False

    if block.none_conditions is not None:
        has_conditions = True
        if any(evaluate_match_spec(s, input_data) for s in block.none_conditions):
            return False

    return has_conditions or (
        block.all_conditions is None and block.any_conditions is None and block.none_conditions is None
    )


def evaluate_match_spec(spec: MatchSpec, input_data: RuleEngineInput) -> bool:
    """Evaluate a single MatchSpec against input data."""
    value = resolve_field(spec.field, input_data)

    if spec.operator == "present":
        return value is not None
    if spec.operator == "absent":
        return value is None
    if spec.operator == "eq":
        return value == spec.value
    if spec.operator == "in":
        if isinstance(spec.value, list):
            return value in spec.value
        return False
    if spec.operator in ("gte", "lte", "gt", "lt"):
        if value is None or spec.value is None:
            return False
        try:
            v = float(value)
            sv = float(spec.value)
        except (TypeError, ValueError):
            return False
        if spec.operator == "gte":
            return v >= sv
        if spec.operator == "lte":
            return v <= sv
        if spec.operator == "gt":
            return v > sv
        if spec.operator == "lt":
            return v < sv

    return False


def resolve_field(field_path: str, input_data: RuleEngineInput) -> str | int | float | bool | None:
    """Resolve a dotted field path to a value from RuleEngineInput.

    Examples:
        "content_type" → input_data.content_type
        "metadata.student_id" → input_data.metadata["student_id"]
        "signals.word_count" → input_data.signals["word_count"]
        "pii_detected" → input_data.pii_detected
    """
    parts = field_path.split(".", 1)
    top = parts[0]

    # Top-level fields
    if top == "content_type":
        return input_data.content_type
    if top == "pii_detected":
        return input_data.pii_detected
    if top == "source":
        return input_data.source
    if top == "content":
        return input_data.content
    if top == "tags":
        # "tags" as a field returns comma-joined string for matching
        return ",".join(input_data.tags) if input_data.tags else None

    # Dotted paths into dicts
    if len(parts) == 2:
        sub_key = parts[1]
        if top == "metadata" and input_data.metadata:
            return input_data.metadata.get(sub_key)
        if top == "signals" and input_data.signals:
            return input_data.signals.get(sub_key)

    return None


def interpolate_template(template: str, input_data: RuleEngineInput) -> str:
    """Interpolate {metadata.student_id} style templates.

    Uses resolve_field for each {placeholder}. Leaves unresolved placeholders intact.
    """

    def _replace(match: re.Match[str]) -> str:
        field_path = match.group(1)
        value = resolve_field(field_path, input_data)
        if value is None:
            return match.group(0)  # Leave unresolved
        return str(value)

    return _TEMPLATE_PATTERN.sub(_replace, template)
