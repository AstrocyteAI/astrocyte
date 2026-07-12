from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from astrocyte.types import (
    Entity,
    MemoryHit,
)

if TYPE_CHECKING:
    from astrocyte.mip.schema import PipelineSpec



_logger = logging.getLogger("astrocyte.mip")


def _warn_on_version_drift(
    bank_pipeline: PipelineSpec | None,
    hits: list[MemoryHit],
    bank_id: str,
) -> None:
    """Emit a single warning when retrieved hits were retained under a different MIP version.

    Soft signal — does not affect recall results. Hits without a persisted
    ``_mip.pipeline_version`` are ignored (they were retained before MIP, by a
    rule with no version, or by a different rule).
    """
    if bank_pipeline is None or bank_pipeline.version is None:
        return
    current_version = int(bank_pipeline.version)
    seen: set[int] = set()
    for hit in hits:
        if not hit.metadata:
            continue
        v = hit.metadata.get("_mip.pipeline_version")
        if v is None:
            continue
        try:
            v_int = int(v)
        except (TypeError, ValueError):
            continue
        if v_int != current_version:
            seen.add(v_int)
    if seen:
        _logger.warning(
            "MIP pipeline version drift in bank %r: current=%d, hits retained under versions %s. "
            "Consider re-indexing or accepting the drift.",
            bank_id,
            current_version,
            sorted(seen),
        )


def _abstention_floor_for_skepticism(skepticism: int, base_floor: float) -> float:
    """Scale the configured abstention floor by a bank/call's
    disposition skepticism (1–5).

    Same primitive Hindsight uses for per-bank disposition behavior:
    a single deployment can serve adversarial-resistant agents AND
    trust-the-model assistants without forking the YAML config. The
    bench harnesses use this to express "LME wants no abstention,
    LoCoMo wants aggressive abstention" via per-call ``dispositions``,
    not split configs.

    Mapping (linear around the legacy default of skepticism=3):

    - ``skepticism=1`` → ``0.0`` — never abstain. An "answer-everything"
      assistant. Replicates the legacy ``abstention_enabled=False`` knob.
    - ``skepticism=2`` → ``base_floor * 0.5`` — trusting, abstain only
      on extremely weak retrieval.
    - ``skepticism=3`` → ``base_floor`` — legacy default. Replicates
      the legacy ``abstention_enabled=True`` behaviour.
    - ``skepticism=4`` → ``base_floor * 1.5`` — moderately skeptical.
    - ``skepticism=5`` → ``base_floor * 2.0`` (capped at 1.0) —
      aggressive abstention. Use for adversarial-bucket evaluation
      where false answers cost more than abstentions.
    """
    if skepticism <= 1:
        return 0.0
    return min(1.0, base_floor * 0.5 * (skepticism - 1))


def _build_cooccurrence_pairs(
    entity_ids: list[str],
    max_entities: int,
) -> list[tuple[str, str]]:
    """Return the (a, b) pairs to create ``co_occurs`` links for.

    Caps the input at ``max_entities`` (taking the head of the list,
    which preserves extraction order — typically tracks prominence in
    the source text), then emits the all-pairs Cartesian product over
    that subset. ``C(K,2)`` pairs at most. Returns ``[]`` for trivial
    inputs (≤1 entity) so the caller can skip the storage call entirely.

    Extracted as a pure helper so the cap is testable without standing
    up the full retain pipeline. The 2026-05-06 LME profile measured
    the unbounded path at 34% of retain wall — capping here is the
    targeted fix.
    """
    if len(entity_ids) <= 1:
        return []
    k = max(2, max_entities)
    head = entity_ids[:k]
    return [(head[i], head[j]) for i in range(len(head)) for j in range(i + 1, len(head))]


def _resolve_skepticism_for_abstention(
    request_dispositions,
    fallback_enabled: bool,
) -> int:
    """Resolve effective skepticism for the abstention decision.

    Precedence:
    1. ``request_dispositions.skepticism`` (per-call override)
    2. Backward compat: legacy
       ``adversarial_defense.abstention_enabled`` bool maps to
       skepticism=3 when True (legacy default behaviour) or
       skepticism=1 when False (never abstain). New code should pass
       ``dispositions`` per-call instead of toggling the bool.
    """
    if request_dispositions is not None:
        return int(getattr(request_dispositions, "skepticism", 3))
    return 3 if fallback_enabled else 1


def _source_ids_from_metadata(metadata: dict[str, Any] | None) -> list[str]:
    if not metadata:
        return []
    raw = metadata.get("_obs_source_ids") or metadata.get("_wiki_source_ids")
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x]
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    return []


def _deterministic_names(text: str) -> set[str]:
    return {match.group(0).strip().lower() for match in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b", text or "")}


def _entities_from_metadata(metadata: dict[str, Any] | None) -> list[Entity]:
    """Build stable entities from structured retain metadata without an LLM call."""

    if not metadata:
        return []
    names: set[str] = set()
    for key in ("locomo_persons", "locomo_speakers", "person"):
        value = metadata.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            names.update(part.strip() for part in value.replace("|", ",").split(",") if part.strip())
        elif isinstance(value, list):
            names.update(str(part).strip() for part in value if str(part).strip())

    entities: list[Entity] = []
    for name in sorted(names):
        entity_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if not entity_id:
            continue
        entities.append(
            Entity(
                id=f"person:{entity_id}",
                name=name,
                entity_type="PERSON",
                aliases=[],
                metadata={"source": "retain_metadata"},
            )
        )
    return entities
