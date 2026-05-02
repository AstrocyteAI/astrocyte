"""Fact-level cause→effect link extraction (Hindsight parity).

This is the C2 rewrite of ``causal_extraction.py``. The key change is
**granularity**: Hindsight extracts causal relations between FACTS
(full-text statements with rich context), not between entities.
``"burnout"`` → ``"resignation"`` is much weaker than
``"Alice was burned out from 80-hour weeks"`` →
``"Alice quit"`` because the latter preserves the textual evidence
that makes causal reasoning useful at recall time.

Hindsight's wire shape (from ``hindsight-api-slim/.../fact_extraction.py``):

    class FactCausalRelation:
        target_fact_index: int        # 0-based index in same batch
        relation_type: Literal["caused_by"]

    class ExtractedFact:
        text: str
        causal_relations: list[FactCausalRelation] | None

In our retain pipeline, "facts" map to chunks (one VectorItem each
after chunking). This module:

1. Takes a batch of chunk texts from the same retain call.
2. Calls the LLM once to identify cause→effect pairs by chunk INDEX.
3. After the orchestrator stores chunks (assigning memory IDs), the
   helper :func:`build_memory_links_from_relations` resolves the
   indices to memory IDs and produces :class:`MemoryLink` objects.
4. Persisted via ``GraphStore.store_memory_links``.

Hindsight uses a single ``"caused_by"`` relation_type only. We follow
that constraint.

Failure modes mirror the entity-level version:

- LLM call failure → return ``[]`` (retain never aborts).
- Malformed JSON → log + return ``[]``.
- ``target_fact_index`` out of range → drop that pair.
- Self-loops (source == target) → drop.
- Duplicate pairs → dedupe.
- Cap on max pairs (default ``2 × num_chunks``, matches Hindsight's
  "max 2 per fact" guideline).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from astrocyte.types import MemoryLink, Message

_logger = logging.getLogger("astrocyte.fact_causal_extraction")


@dataclass
class FactCausalRelation:
    """Cause→effect relation between two facts in the same batch."""

    source_fact_index: int  # the EFFECT (this fact was caused_by target)
    target_fact_index: int  # the CAUSE
    evidence: str = ""
    confidence: float = 1.0


_SYSTEM_PROMPT = """\
You extract cause→effect relationships between facts in a numbered batch.

Each fact has an index (0-based). For each fact, identify which OTHER \
facts in the batch caused it. Output ONLY pairs that are EXPLICITLY \
causal in the source — never inferred.

Output a JSON array. Each element is:
  {"source_fact_index": <int>, "target_fact_index": <int>, \
"relation_type": "caused_by", "evidence": "<verbatim quote, ≤ 20 words>", \
"confidence": <0.0-1.0>}

The semantics: ``source_fact_index`` was caused_by ``target_fact_index``. \
target must be a DIFFERENT index than source. target may be in any \
position relative to source (no ordering constraint — sometimes the \
cause is mentioned later in the source text).

Rules:
1. Look for explicit causal language: "because", "caused", "led to", \
"resulted in", "due to", "triggered", "as a result", "made him/her X", \
"after X happened, Y happened" (only when the temporal sequence + \
outcome implies causation).
2. Confidence ≥ 0.8 only when text explicitly states causation. \
0.6-0.8 for strongly-implied. Below 0.6: skip.
3. Evidence MUST be a verbatim quote from the input text (≤ 20 words). \
Don't paraphrase.
4. Never relate a fact to itself.
5. Maximum 2 causes per fact.
6. If no causal relationships are stated, return [].

Output JSON only. No prose.
"""


def _build_user_prompt(chunk_texts: list[str]) -> str:
    lines = ["Facts (numbered):"]
    for idx, text in enumerate(chunk_texts):
        # Trim each chunk so prompt size stays bounded; preserves the
        # first ~600 chars which is plenty for causal-text detection.
        snippet = text.strip()
        if len(snippet) > 600:
            snippet = snippet[:597] + "..."
        lines.append(f"[{idx}] {snippet}")
    lines.append("")
    lines.append("Causal pairs (JSON array):")
    return "\n".join(lines)


def _parse_relations(raw: str) -> list[dict]:
    """Pull the first JSON array from the LLM response.

    Tolerates ``` fences and surrounding prose. Returns ``[]`` on any
    failure — retain never aborts on causal extraction.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match is None:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        _logger.warning("fact_causal_extraction: JSON decode failed (%s)", exc)
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


async def extract_fact_causal_relations(
    chunk_texts: list[str],
    llm_provider,
    *,
    max_pairs_per_fact: int = 2,
    min_confidence: float = 0.6,
) -> list[FactCausalRelation]:
    """Identify cause→effect pairs among the supplied chunks.

    Returns relations as ``(source_fact_index, target_fact_index)``
    tuples in :class:`FactCausalRelation` form. The orchestrator
    converts these to :class:`MemoryLink` objects after storage
    assigns memory IDs to the chunks.
    """
    if len(chunk_texts) < 2:
        return []

    try:
        completion = await llm_provider.complete(
            [
                Message(role="system", content=_SYSTEM_PROMPT),
                Message(role="user", content=_build_user_prompt(chunk_texts)),
            ],
            max_tokens=1024,
            temperature=0.0,
        )
    except Exception as exc:
        _logger.warning("fact_causal_extraction: LLM call failed (%s)", exc)
        return []

    parsed = _parse_relations(completion.text)
    if not parsed:
        return []

    n = len(chunk_texts)
    out: list[FactCausalRelation] = []
    seen: set[tuple[int, int]] = set()
    per_source_count: dict[int, int] = {}
    for raw in parsed:
        try:
            src_idx = int(raw.get("source_fact_index"))
            tgt_idx = int(raw.get("target_fact_index"))
        except (TypeError, ValueError):
            continue
        if src_idx == tgt_idx:
            continue
        if not (0 <= src_idx < n) or not (0 <= tgt_idx < n):
            continue
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < min_confidence:
            continue
        if per_source_count.get(src_idx, 0) >= max_pairs_per_fact:
            continue

        key = (src_idx, tgt_idx)
        if key in seen:
            continue
        seen.add(key)
        per_source_count[src_idx] = per_source_count.get(src_idx, 0) + 1

        evidence = str(raw.get("evidence") or "").strip()[:500]
        out.append(
            FactCausalRelation(
                source_fact_index=src_idx,
                target_fact_index=tgt_idx,
                evidence=evidence,
                confidence=confidence,
            )
        )
    return out


def build_memory_links_from_relations(
    relations: list[FactCausalRelation],
    memory_ids: list[str],
    *,
    bank_id: str,
) -> list[MemoryLink]:
    """Resolve fact indices to memory IDs and build :class:`MemoryLink`.

    Source = effect, target = cause (matches Hindsight's ``caused_by``
    semantics). Drops relations whose indices fall outside the supplied
    ``memory_ids`` range — defensive against off-by-one errors when
    chunking changes count.
    """
    out: list[MemoryLink] = []
    n = len(memory_ids)
    now = datetime.now(timezone.utc)
    for rel in relations:
        if not (0 <= rel.source_fact_index < n) or not (0 <= rel.target_fact_index < n):
            continue
        out.append(
            MemoryLink(
                source_memory_id=memory_ids[rel.source_fact_index],
                target_memory_id=memory_ids[rel.target_fact_index],
                link_type="caused_by",
                evidence=rel.evidence,
                confidence=rel.confidence,
                weight=1.0,
                created_at=now,
                metadata={"bank_id": bank_id, "source": "fact_causal_extraction"},
            )
        )
    return out
