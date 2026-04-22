"""MIP pipeline presets — named bundles of chunker/dedup/rerank/reflect overrides.

Presets are the **primary** authoring interface for pipeline shaping (P1).
Authors write `pipeline: { preset: conversational }` rather than picking
individual knobs. Raw overrides are supported but documented as advanced.

Expansion happens once at load time (in loader._parse_pipeline). Downstream
code only ever sees fully-resolved PipelineSpec instances — never preset names.

To add a preset: add an entry to PRESETS. Update docs/_plugins/mip-developer-guide.md.
"""

from __future__ import annotations

from dataclasses import replace

from astrocyte.mip.schema import (
    ChunkerSpec,
    DedupSpec,
    ForgetSpec,
    PipelineSpec,
    ReflectSpec,
    RerankSpec,
)

PRESETS: dict[str, PipelineSpec] = {
    "conversational": PipelineSpec(
        chunker=ChunkerSpec(strategy="dialogue", max_size=800, overlap=0),
        dedup=DedupSpec(threshold=0.92, action="skip_chunk"),
        rerank=RerankSpec(keyword_weight=0.08, proper_noun_weight=0.15),
        reflect=ReflectSpec(prompt="temporal_aware", promote_metadata=["speaker", "occurred_at"]),
    ),
    "document": PipelineSpec(
        chunker=ChunkerSpec(strategy="paragraph", max_size=1200, overlap=100),
        dedup=DedupSpec(threshold=0.95, action="skip"),
        rerank=RerankSpec(keyword_weight=0.10, proper_noun_weight=0.05),
        reflect=ReflectSpec(prompt="default", promote_metadata=None),
    ),
    "code": PipelineSpec(
        chunker=ChunkerSpec(strategy="fixed", max_size=1500, overlap=200),
        dedup=DedupSpec(threshold=0.98, action="skip"),
        rerank=RerankSpec(keyword_weight=0.12, proper_noun_weight=0.0),
        reflect=ReflectSpec(prompt="evidence_strict", promote_metadata=None),
    ),
    "evidence_strict": PipelineSpec(
        # Inherits caller's chunker (no override)
        chunker=None,
        dedup=DedupSpec(threshold=0.98, action="skip"),
        rerank=RerankSpec(keyword_weight=0.10, proper_noun_weight=0.05),
        reflect=ReflectSpec(prompt="evidence_strict", promote_metadata=["source", "occurred_at"]),
    ),
}


def is_known_preset(name: str) -> bool:
    return name in PRESETS


def list_presets() -> list[str]:
    return sorted(PRESETS.keys())


# ---------------------------------------------------------------------------
# Forget presets (Phase 4)
# ---------------------------------------------------------------------------

FORGET_PRESETS: dict[str, ForgetSpec] = {
    # GDPR right-to-erasure: hard delete, audit required, cascade derived data,
    # legal hold MUST be respected (compliance-mandated).
    "gdpr": ForgetSpec(
        mode="hard",
        audit="required",
        cascade=True,
        respect_legal_hold=True,
        min_age_days=0,
    ),
    # Student records (FERPA-style): soft delete with grace period, audit on,
    # refuse on records < 7 days old to prevent accidents.
    "student": ForgetSpec(
        mode="soft",
        audit="recommended",
        cascade=True,
        respect_legal_hold=True,
        min_age_days=7,
    ),
    # Audit-strict: tombstone replacement (preserves cryptographic chain),
    # audit required, cascade off (each tombstone tracked individually).
    "audit-strict": ForgetSpec(
        mode="tombstone",
        audit="required",
        cascade=False,
        respect_legal_hold=True,
        min_age_days=0,
    ),
}


def is_known_forget_preset(name: str) -> bool:
    return name in FORGET_PRESETS


def list_forget_presets() -> list[str]:
    return sorted(FORGET_PRESETS.keys())


def expand_forget_preset(spec: ForgetSpec) -> ForgetSpec:
    """Merge a forget preset (if named) with explicit overrides on the spec.

    Explicit fields on ``spec`` take precedence over preset defaults. Returns
    a new :class:`ForgetSpec` with ``preset`` cleared and all fields resolved.
    Caller is responsible for raising on unknown presets.
    """
    if spec.preset is None:
        return spec
    base = FORGET_PRESETS[spec.preset]
    return ForgetSpec(
        version=spec.version,
        preset=None,
        mode=spec.mode if spec.mode is not None else base.mode,
        audit=spec.audit if spec.audit is not None else base.audit,
        cascade=spec.cascade if spec.cascade is not None else base.cascade,
        respect_legal_hold=(
            spec.respect_legal_hold if spec.respect_legal_hold is not None
            else base.respect_legal_hold
        ),
        min_age_days=spec.min_age_days if spec.min_age_days is not None else base.min_age_days,
        max_per_call=spec.max_per_call if spec.max_per_call is not None else base.max_per_call,
    )


def expand_preset(spec: PipelineSpec) -> PipelineSpec:
    """Merge a preset (if named) with explicit overrides on the spec.

    Explicit fields on `spec` take precedence over preset defaults. Returns
    a new PipelineSpec with `preset` cleared and all sub-blocks resolved.

    If `spec.preset` is None, returns `spec` unchanged (raw overrides only).
    Caller is responsible for raising on unknown presets — use is_known_preset
    during loader validation so the error mentions the rule name.
    """
    if spec.preset is None:
        return spec

    base = PRESETS[spec.preset]

    return PipelineSpec(
        version=spec.version,
        preset=None,  # cleared post-expansion
        chunker=_merge_chunker(base.chunker, spec.chunker),
        dedup=_merge_dedup(base.dedup, spec.dedup),
        rerank=_merge_rerank(base.rerank, spec.rerank),
        reflect=_merge_reflect(base.reflect, spec.reflect),
        # Explicit override wins over preset default; preset defaults
        # don't currently set half-life but the field is forward-compatible
        # if a future preset does.
        temporal_half_life_days=(
            spec.temporal_half_life_days
            if spec.temporal_half_life_days is not None
            else base.temporal_half_life_days
        ),
    )


def _merge_chunker(base: ChunkerSpec | None, override: ChunkerSpec | None) -> ChunkerSpec | None:
    if override is None:
        return base
    if base is None:
        return override
    return replace(
        base,
        strategy=override.strategy if override.strategy is not None else base.strategy,
        max_size=override.max_size if override.max_size is not None else base.max_size,
        overlap=override.overlap if override.overlap is not None else base.overlap,
    )


def _merge_dedup(base: DedupSpec | None, override: DedupSpec | None) -> DedupSpec | None:
    if override is None:
        return base
    if base is None:
        return override
    return replace(
        base,
        threshold=override.threshold if override.threshold is not None else base.threshold,
        action=override.action if override.action is not None else base.action,
    )


def _merge_rerank(base: RerankSpec | None, override: RerankSpec | None) -> RerankSpec | None:
    if override is None:
        return base
    if base is None:
        return override
    return replace(
        base,
        keyword_weight=(
            override.keyword_weight if override.keyword_weight is not None else base.keyword_weight
        ),
        proper_noun_weight=(
            override.proper_noun_weight
            if override.proper_noun_weight is not None
            else base.proper_noun_weight
        ),
    )


def _merge_reflect(base: ReflectSpec | None, override: ReflectSpec | None) -> ReflectSpec | None:
    if override is None:
        return base
    if base is None:
        return override
    return replace(
        base,
        prompt=override.prompt if override.prompt is not None else base.prompt,
        promote_metadata=(
            override.promote_metadata
            if override.promote_metadata is not None
            else base.promote_metadata
        ),
    )
