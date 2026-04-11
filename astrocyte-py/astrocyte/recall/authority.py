"""Structured recall authority — label hits by tier for precedence-in-the-prompt (M7).

Hits are grouped using ``MemoryHit.metadata["authority_tier"]`` matching
:class:`~astrocyte.config.RecallAuthorityTierConfig` ``id``. Unmatched hits appear in a final
``[UNASSIGNED]`` section. Multi-query retrieval per tier is a later phase; this module formats
a single fused ``RecallResult``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from astrocyte.config import RecallAuthorityConfig
from astrocyte.types import MemoryHit, Metadata, RecallResult

_METADATA_KEY = "authority_tier"


def merge_retain_metadata_authority_tier(
    metadata: Metadata | None,
    *,
    bank_id: str,
    profile_authority_tier: str | None,
    recall_authority: RecallAuthorityConfig | None,
) -> Metadata | None:
    """Set ``metadata[\"authority_tier\"]`` from extraction profile or ``tier_by_bank`` (M7 producers)."""
    if recall_authority is None or not recall_authority.enabled:
        return metadata
    tier: str | None = None
    if profile_authority_tier and str(profile_authority_tier).strip():
        tier = str(profile_authority_tier).strip()
    elif recall_authority.tier_by_bank:
        raw = recall_authority.tier_by_bank.get(bank_id)
        tier = str(raw).strip() if raw else None
    if not tier:
        return metadata
    out: Metadata = dict(metadata or {})
    out[_METADATA_KEY] = tier
    return out


def load_authority_rules(cfg: RecallAuthorityConfig) -> str:
    """Return rules text from ``rules_inline`` or ``rules_path`` (file UTF-8)."""
    if cfg.rules_inline and str(cfg.rules_inline).strip():
        return str(cfg.rules_inline).strip()
    if cfg.rules_path and str(cfg.rules_path).strip():
        path = Path(cfg.rules_path)
        return path.read_text(encoding="utf-8").strip()
    return ""


def build_authority_context(cfg: RecallAuthorityConfig, hits: list[MemoryHit]) -> str:
    """Build a single string with priority-ordered sections for model context."""
    rules = load_authority_rules(cfg)
    tiers_sorted = sorted(cfg.tiers, key=lambda t: (t.priority, t.id))
    buckets: dict[str, list[MemoryHit]] = {t.id: [] for t in tiers_sorted}
    unassigned: list[MemoryHit] = []

    for h in hits:
        md = h.metadata or {}
        raw = md.get(_METADATA_KEY)
        key = str(raw).strip() if raw is not None else ""
        if key and key in buckets:
            buckets[key].append(h)
        else:
            unassigned.append(h)

    lines: list[str] = []
    if rules:
        lines.append(rules)
        lines.append("")
        lines.append("---")
        lines.append("")

    for t in tiers_sorted:
        label = t.label.strip() if t.label else f"[{t.id}]"
        lines.append(label)
        section_hits = buckets.get(t.id, [])
        if not section_hits:
            lines.append("(no hits in this tier)")
        else:
            for h in section_hits:
                lines.append(f"- {h.text.strip()}")
        lines.append("")

    if unassigned:
        lines.append("[UNASSIGNED]")
        for h in unassigned:
            lines.append(f"- {h.text.strip()}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def apply_recall_authority(result: RecallResult, cfg: RecallAuthorityConfig | None) -> RecallResult:
    """Attach ``authority_context`` when ``recall_authority.enabled`` and tiers are configured."""
    if cfg is None or not cfg.enabled:
        return result
    if not cfg.tiers:
        return replace(result, authority_context=load_authority_rules(cfg) or None)
    text = build_authority_context(cfg, result.hits)
    return replace(result, authority_context=text or None)
