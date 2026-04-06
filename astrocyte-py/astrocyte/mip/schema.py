"""MIP schema — all dataclasses for MIP configuration.

FFI-safe: no Any, no callables.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BankDefinition:
    id: str  # May contain templates like "student-{student_id}"
    description: str | None = None
    access: list[str] | None = None  # ["agent:tutor", "agent:grader"]
    compliance: str | None = None  # "pdpa", "gdpr", etc.


@dataclass
class MatchSpec:
    """A single match condition."""

    field: str  # "content_type", "metadata.student_id", "pii_detected"
    operator: str  # "eq", "in", "gte", "lte", "gt", "lt", "present", "absent"
    value: str | int | float | bool | list[str] | None = None


@dataclass
class MatchBlock:
    """Boolean composition of match conditions."""

    all_conditions: list[MatchSpec] | None = None
    any_conditions: list[MatchSpec] | None = None
    none_conditions: list[MatchSpec] | None = None


@dataclass
class ActionSpec:
    bank: str | None = None  # May contain templates: "student-{metadata.student_id}"
    tags: list[str] | None = None  # May contain templates
    retain_policy: str | None = None  # "default" | "redact_before_store" | "encrypt" | "reject"
    escalate: str | None = None  # "mip" or None
    confidence: float = 1.0


@dataclass
class RoutingRule:
    name: str
    priority: int
    match: MatchBlock
    action: ActionSpec
    override: bool = False  # Compliance-mandatory, cannot be overridden by intent


@dataclass
class EscalationCondition:
    condition: str  # "matched_rules", "confidence", "conflicting_rules"
    operator: str  # "eq", "lt", "gt", "gte", "lte"
    value: str | int | float | bool = 0


@dataclass
class IntentPolicy:
    escalate_when: list[EscalationCondition] | None = None
    model_context: str | None = None  # Prompt template with {banks}, {tags}
    constraints: dict[str, list[str] | bool | int] | None = None
    # constraints keys: "cannot_override" (list[str]), "must_justify" (bool), "max_tokens" (int)


@dataclass
class MipConfig:
    version: str = "1.0"
    banks: list[BankDefinition] | None = None
    rules: list[RoutingRule] | None = None
    intent_policy: IntentPolicy | None = None
