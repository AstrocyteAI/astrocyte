"""MIP schema — all dataclasses for MIP configuration.

FFI-safe: no Any, no callables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
class ChunkerSpec:
    """Per-rule chunker override. Absent fields fall back to ExtractionProfileConfig."""

    strategy: str | None = None  # "sentence" | "dialogue" | "paragraph" | "fixed"
    max_size: int | None = None
    overlap: int | None = None


@dataclass
class DedupSpec:
    """Per-rule dedup override. Absent fields fall back to DedupConfig."""

    threshold: float | None = None  # 0.0–1.0
    action: str | None = None  # "skip" | "skip_chunk" | "warn" | "update"


@dataclass
class RerankSpec:
    """Per-rule reranker override. Resolved per-bank at recall time (P3)."""

    keyword_weight: float | None = None
    proper_noun_weight: float | None = None


@dataclass
class ReflectSpec:
    """Per-rule reflect override. Resolved at synthesis time."""

    prompt: str | None = None  # "default" | "temporal_aware" | "evidence_strict"
    promote_metadata: list[str] | None = None  # capped at 5 fields (P4)


@dataclass
class PipelineSpec:
    """Pipeline-shaping action vocabulary. All sub-blocks optional.

    `version` is required when any pipeline field is set (P2). Persisted onto
    each retained record so recall can warn on rule-version drift.

    `preset` expands at load time into the explicit sub-block fields. Explicit
    fields override preset defaults.
    """

    version: int | None = None
    preset: str | None = None  # "conversational" | "document" | "code" | "evidence_strict"
    chunker: ChunkerSpec | None = None
    dedup: DedupSpec | None = None
    rerank: RerankSpec | None = None
    reflect: ReflectSpec | None = None


@dataclass
class ForgetSpec:
    """Per-rule forget policy. Resolved at forget time, keyed by target bank.

    All fields optional; absent fields fall back to caller-supplied arguments
    or library defaults. ``version`` is required when any field is set (P2),
    same semantics as :class:`PipelineSpec`.
    """

    version: int | None = None
    preset: str | None = None  # "gdpr" | "student" | "audit-strict"
    mode: str | None = None  # "soft" | "hard" | "tombstone"
    audit: str | None = None  # "none" | "recommended" | "required"
    cascade: bool | None = None  # cascade delete derived chunks/embeddings
    respect_legal_hold: bool | None = None  # refuse forget if legal hold present
    min_age_days: int | None = None  # refuse forget on records younger than N days
    max_per_call: int | None = None  # cap on records per forget request


@dataclass
class ActionSpec:
    bank: str | None = None  # May contain templates: "student-{metadata.student_id}"
    tags: list[str] | None = None  # May contain templates
    retain_policy: str | None = None  # "default" | "redact_before_store" | "encrypt" | "reject"
    escalate: str | None = None  # "mip" or None
    confidence: float = 1.0
    pipeline: PipelineSpec | None = None  # Optional pipeline-shaping overrides
    forget: ForgetSpec | None = None  # Optional forget-policy overrides (Phase 4)


@dataclass
class RoutingRule:
    name: str
    priority: int
    match: MatchBlock
    action: ActionSpec
    override: bool = False  # Compliance-mandatory, cannot be overridden by intent
    # Phase 5 — operator ergonomics
    #: Shadow mode: rule is evaluated and logged but its action is NOT applied.
    #: Used to canary-test new rules with zero behavioral impact.
    shadow: bool = False
    #: Activation window. If now < active_from or now > active_until the rule
    #: is skipped (treated as not present). Useful for staged rollouts.
    active_from: datetime | None = None
    active_until: datetime | None = None
    #: Free-form labels surfaced on RoutingDecision and structured logs so
    #: operators can group metrics by rule purpose ("compliance", "experiment").
    observability_tags: list[str] | None = None


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
    #: Phase 5 — how to resolve multiple matches at the same priority.
    #: ``"first"`` (default) preserves declaration order; ``"error"`` raises
    #: MipRoutingError so authoring conflicts surface loudly; ``"most_specific"``
    #: picks the rule with the most match conditions.
    tie_breaker: str = "first"
