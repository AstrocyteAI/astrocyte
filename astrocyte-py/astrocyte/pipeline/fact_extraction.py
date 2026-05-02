"""Structured fact extraction at retain time.

Replaces the legacy two-pass approach (text chunking → separate
entity-extraction LLM call → separate fact-causal LLM call) with a
single LLM call that produces a list of structured facts. Each fact
carries five dimensions plus its embedded entities and intra-batch
causal relations:

- ``what`` — the core factual content (1–2 sentences)
- ``when`` — temporal expression in natural language plus optional
  ISO ``occurred_start`` / ``occurred_end``
- ``where`` — location or N/A
- ``who`` — people involved + relationships
- ``why`` — context or N/A
- ``fact_type`` — ``"world"`` (objective external fact) vs
  ``"experience"`` (first-person event)
- ``entities`` — named entities embedded in the fact
- ``causal_relations`` — directional ``caused_by`` references to other
  facts in the SAME batch by index

Why this matters: every downstream signal (semantic embedding, cross-
encoder rerank, link expansion, reflect synthesis) gets cleaner inputs
when ``who`` and ``when`` are first-class fields rather than buried in
free text. Multi-hop and temporal questions especially benefit because
the structured fields can be filtered / matched directly.

Cost: single LLM call per retain text replaces two (entity + causal).
Net cost approximately equal; output substantially richer.

The module also exposes :func:`materialize_facts` which converts a
list of :class:`ExtractedFact` objects into the three artefacts the
orchestrator needs to persist:

- :class:`VectorItem` per fact (replaces chunk-based VectorItems)
- :class:`Entity` per unique entity mentioned across the batch
- :class:`MemoryLink` per causal relation (memory-to-memory edges)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from astrocyte.types import (
    Entity,
    MemoryLink,
    Message,
    VectorItem,
)

_logger = logging.getLogger("astrocyte.fact_extraction")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class FactEntity:
    """An entity mentioned within a fact."""

    name: str
    entity_type: str = "OTHER"  # PERSON, ORG, LOCATION, CONCEPT, OTHER


@dataclass
class FactCausalRelation:
    """``caused_by`` reference from THIS fact to another fact in the
    same batch. Hindsight-style: target_index < source_index is
    enforced upstream when building MemoryLinks (we drop self-loops
    and out-of-range references)."""

    target_fact_index: int
    strength: float = 1.0  # 0..1, defaults to strong


@dataclass
class ExtractedFact:
    """A single structured fact extracted from retain text.

    The 5-dimension schema (what/when/where/who/why) materializes the
    semantic content the LLM extracted, plus embedded entities and
    causal links between facts in the same batch.
    """

    what: str  # core content
    when: str = "N/A"  # natural-language time expression
    where: str = "N/A"
    who: str = "N/A"
    why: str = "N/A"
    fact_type: str = "experience"  # "world" | "experience"
    occurred_start: datetime | None = None
    occurred_end: datetime | None = None
    entities: list[FactEntity] = field(default_factory=list)
    causal_relations: list[FactCausalRelation] = field(default_factory=list)

    def build_text(self) -> str:
        """Combine dimensions into a single fact text for storage.

        Format: ``"{what} | Involving: {who} | {why}"`` with N/A
        sections dropped. Hindsight uses the same convention.
        """
        parts: list[str] = [self.what.strip()]
        if self.who and self.who.upper() != "N/A":
            parts.append(f"Involving: {self.who.strip()}")
        if self.why and self.why.upper() != "N/A":
            parts.append(self.why.strip())
        if self.where and self.where.upper() != "N/A":
            parts.append(f"At: {self.where.strip()}")
        if self.when and self.when.upper() != "N/A":
            parts.append(f"When: {self.when.strip()}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


_VERBATIM_SYSTEM_PROMPT = """\
You enrich PRE-CHUNKED text with structured metadata. Do NOT rewrite \
or paraphrase the chunk text — the caller will use the original chunk \
text verbatim. Your job is to produce per-chunk metadata.

Output a JSON object: {"facts": [...]}. The facts list MUST be the \
same length as the input chunks list, in the same order. For each \
chunk, produce ONE entry with:
- "what": leave EMPTY (""). The caller substitutes the original chunk \
text. Including content here is wasted output tokens.
- "when" (string, default "N/A"): natural-language time expression \
present in this chunk; "N/A" otherwise.
- "where" (string, default "N/A"): location.
- "who" (string, default "N/A"): people involved.
- "why" (string, default "N/A"): reason / motivation if explicit.
- "fact_type" ("world" | "experience"): classify the chunk.
- "occurred_start" (ISO 8601 string or null): resolve "when" to an \
absolute date when possible; null otherwise.
- "occurred_end" (ISO 8601 string or null): instant or end of range.
- "entities" (list): each entity mentioned in this chunk, as \
{"name": str, "entity_type": str}. Types: PERSON, ORG, LOCATION, \
PRODUCT, CONCEPT, OTHER.
- "causal_relations" (list, default []): each entry is \
{"target_fact_index": int, "strength": float} pointing at ANOTHER \
chunk in the input that THIS chunk was caused_by. ``target_fact_index`` \
references the chunk's position in the input list.

Rules:
1. Output exactly one entry per input chunk, in the same order.
2. NEVER include content in "what" — leave it empty.
3. Don't invent. Use "N/A" / null / [] for absent metadata.
4. Output JSON only.
"""


def _build_verbatim_user_prompt(
    chunk_texts: list[str], event_date: datetime | None = None,
) -> str:
    lines = []
    if event_date is not None:
        lines.append(f"Reference date for relative time expressions: {event_date.isoformat()}")
        lines.append("")
    lines.append(f"Chunks ({len(chunk_texts)} total, indexed):")
    for i, text in enumerate(chunk_texts):
        snippet = text.strip()
        if len(snippet) > 800:
            snippet = snippet[:797] + "..."
        lines.append(f"[{i}] {snippet}")
    lines.append("")
    lines.append("Per-chunk metadata (JSON, same order, same length):")
    return "\n".join(lines)


_SYSTEM_PROMPT = """\
You extract STRUCTURED FACTS from text. Each fact is a discrete \
factual unit with five dimensions: what / when / where / who / why.

Output a JSON object: {"facts": [...]}. Each fact has:
- "what" (string, REQUIRED): the core fact, 1-2 concise sentences. \
This is the content. Don't repeat who/when/where here.
- "when" (string, default "N/A"): natural-language time expression \
("last spring", "yesterday", "in March 2024", "2 weeks ago"). \
Use "N/A" for stable preferences / general facts with no specific time.
- "where" (string, default "N/A"): location or "N/A".
- "who" (string, default "N/A"): people involved + their roles. \
For first-person facts, name the speaker explicitly.
- "why" (string, default "N/A"): reason / motivation / context, \
but ONLY when the source text explicitly states it.
- "fact_type" ("world" | "experience"): "world" for objective external \
facts ("Google was founded in 1998"); "experience" for first-person \
events / preferences / observations ("Alice prefers Python").
- "occurred_start" (ISO 8601 string or null): when the fact's event \
began. Compute from "when" if it's resolvable to an absolute date.
- "occurred_end" (ISO 8601 string or null): event end time, or null \
for instantaneous events.
- "entities" (list): each entity mentioned in this fact, as \
{"name": str, "entity_type": str}. Types: PERSON, ORG, LOCATION, \
PRODUCT, CONCEPT, OTHER.
- "causal_relations" (list, default []): each entry is \
{"target_fact_index": int, "strength": float}. ``target_fact_index`` \
references the index of ANOTHER fact in this batch that this fact was \
CAUSED_BY. The relation is directional. ``strength`` ∈ [0, 1]; use \
≥ 0.8 only when causation is explicit ("because", "led to"). Skip \
when not in the source.

Rules:
1. Decompose into ATOMIC facts. One sentence per fact, ideally.
2. Preserve every factual claim — don't merge or summarize.
3. Don't invent. If "where" isn't stated, use "N/A". If "why" isn't \
stated, use "N/A".
4. Output JSON only. No prose.
"""


def _build_user_prompt(text: str, event_date: datetime | None = None) -> str:
    lines = []
    if event_date is not None:
        lines.append(f"Reference date for relative time expressions: {event_date.isoformat()}")
        lines.append("")
    lines.append("Source text:")
    lines.append(text.strip())
    lines.append("")
    lines.append("Extracted facts (JSON):")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------


def _parse_json_object(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_iso_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Tolerate trailing 'Z' (Python 3.11+ handles it, but be defensive).
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


async def extract_facts(
    text: str,
    llm_provider,
    *,
    event_date: datetime | None = None,
    max_facts: int = 30,
) -> list[ExtractedFact]:
    """Extract structured 5-dimension facts from text.

    Single LLM call per text input. Returns ``[]`` on any failure
    (LLM error, malformed JSON, missing required fields) so retain
    never aborts.
    """
    if not text or not text.strip():
        return []

    try:
        completion = await llm_provider.complete(
            [
                Message(role="system", content=_SYSTEM_PROMPT),
                Message(role="user", content=_build_user_prompt(text, event_date)),
            ],
            max_tokens=4096,
            temperature=0.0,
        )
    except Exception as exc:
        _logger.warning("fact_extraction LLM call failed (%s)", exc)
        return []

    parsed = _parse_json_object(completion.text)
    if parsed is None:
        _logger.warning("fact_extraction: malformed JSON response")
        return []

    raw_facts = parsed.get("facts")
    if not isinstance(raw_facts, list):
        return []

    out: list[ExtractedFact] = []
    for raw in raw_facts[:max_facts]:
        if not isinstance(raw, dict):
            continue
        what = str(raw.get("what") or "").strip()
        if not what:
            # ``what`` is required; skip incomplete facts.
            continue

        # Entities
        entities: list[FactEntity] = []
        for ent in raw.get("entities") or []:
            if not isinstance(ent, dict):
                continue
            name = str(ent.get("name") or "").strip()
            if not name:
                continue
            etype = str(ent.get("entity_type") or "OTHER").strip().upper() or "OTHER"
            entities.append(FactEntity(name=name, entity_type=etype))

        # Causal relations
        causal: list[FactCausalRelation] = []
        for rel in raw.get("causal_relations") or []:
            if not isinstance(rel, dict):
                continue
            try:
                target = int(rel.get("target_fact_index"))
            except (TypeError, ValueError):
                continue
            try:
                strength = float(rel.get("strength", 1.0))
            except (TypeError, ValueError):
                strength = 1.0
            causal.append(FactCausalRelation(target_fact_index=target, strength=strength))

        ftype = str(raw.get("fact_type") or "experience").strip().lower()
        if ftype not in {"world", "experience"}:
            ftype = "experience"

        out.append(
            ExtractedFact(
                what=what,
                when=str(raw.get("when") or "N/A").strip() or "N/A",
                where=str(raw.get("where") or "N/A").strip() or "N/A",
                who=str(raw.get("who") or "N/A").strip() or "N/A",
                why=str(raw.get("why") or "N/A").strip() or "N/A",
                fact_type=ftype,
                occurred_start=_parse_iso_datetime(raw.get("occurred_start")),
                occurred_end=_parse_iso_datetime(raw.get("occurred_end")),
                entities=entities,
                causal_relations=causal,
            )
        )
    return out


async def extract_facts_verbatim(
    chunk_texts: list[str],
    llm_provider,
    *,
    event_date: datetime | None = None,
) -> list[ExtractedFact]:
    """Extract per-chunk metadata WITHOUT paraphrasing the chunk text.

    The "what" field of each returned :class:`ExtractedFact` is set to
    the original chunk text — not an LLM-generated summary. The LLM's
    job here is only to produce structured metadata (entities,
    causal_relations, temporal range, fact_type, where/who/why
    annotations) per chunk.

    Why this exists (the design lesson from 2026-05-02):
    The "concise" mode :func:`extract_facts` replaces conversation text
    with structured paraphrases like "Caroline went hiking | Involving:
    Caroline | When: yesterday". That paraphrase loses the surface
    vocabulary of the original conversation, which question embeddings
    typically share — causing severe recall_hit_rate degradation. The
    verbatim mode preserves the original vocabulary while still
    enriching each chunk with the structured metadata needed for
    causal/temporal/per-fact retrieval signals.

    Returns one :class:`ExtractedFact` per input chunk, in the same
    order, with ``what`` = the chunk text. Returns ``[]`` on any
    failure so retain falls back to legacy chunking.

    Args:
        chunk_texts: List of pre-chunked source texts; one per memory.
        llm_provider: Producer of the metadata extraction.
        event_date: Reference for resolving relative time expressions.
    """
    if not chunk_texts:
        return []
    # Prefilter: drop empty chunks but preserve indices for the LLM.
    if not any(t.strip() for t in chunk_texts):
        return []

    try:
        completion = await llm_provider.complete(
            [
                Message(role="system", content=_VERBATIM_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=_build_verbatim_user_prompt(chunk_texts, event_date),
                ),
            ],
            max_tokens=4096,
            temperature=0.0,
        )
    except Exception as exc:
        _logger.warning("fact_extraction (verbatim) LLM call failed (%s)", exc)
        return []

    parsed = _parse_json_object(completion.text)
    if parsed is None:
        _logger.warning("fact_extraction (verbatim): malformed JSON response")
        return []
    raw_metadata = parsed.get("facts")
    if not isinstance(raw_metadata, list):
        _logger.warning("fact_extraction (verbatim): 'facts' is not a list")
        return []

    out: list[ExtractedFact] = []
    for idx, chunk in enumerate(chunk_texts):
        # Pull the matching metadata entry by index. When the LLM
        # returned fewer entries than chunks, the trailing chunks get
        # bare metadata-less ExtractedFacts (still preserves chunk text).
        raw = raw_metadata[idx] if idx < len(raw_metadata) else {}
        if not isinstance(raw, dict):
            raw = {}

        # Entities
        entities: list[FactEntity] = []
        for ent in raw.get("entities") or []:
            if not isinstance(ent, dict):
                continue
            name = str(ent.get("name") or "").strip()
            if not name:
                continue
            etype = str(ent.get("entity_type") or "OTHER").strip().upper() or "OTHER"
            entities.append(FactEntity(name=name, entity_type=etype))

        # Causal relations — same semantics as concise mode but indices
        # reference the chunk position (which IS the memory position).
        causal: list[FactCausalRelation] = []
        for rel in raw.get("causal_relations") or []:
            if not isinstance(rel, dict):
                continue
            try:
                target = int(rel.get("target_fact_index"))
            except (TypeError, ValueError):
                continue
            if target == idx or not (0 <= target < len(chunk_texts)):
                continue
            try:
                strength = float(rel.get("strength", 1.0))
            except (TypeError, ValueError):
                strength = 1.0
            causal.append(FactCausalRelation(target_fact_index=target, strength=strength))

        ftype = str(raw.get("fact_type") or "experience").strip().lower()
        if ftype not in {"world", "experience"}:
            ftype = "experience"

        out.append(
            ExtractedFact(
                # KEY: "what" is the ORIGINAL chunk text, not a paraphrase.
                what=chunk,
                when=str(raw.get("when") or "N/A").strip() or "N/A",
                where=str(raw.get("where") or "N/A").strip() or "N/A",
                who=str(raw.get("who") or "N/A").strip() or "N/A",
                why=str(raw.get("why") or "N/A").strip() or "N/A",
                fact_type=ftype,
                occurred_start=_parse_iso_datetime(raw.get("occurred_start")),
                occurred_end=_parse_iso_datetime(raw.get("occurred_end")),
                entities=entities,
                causal_relations=causal,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


@dataclass
class MaterializedFacts:
    """The retain-pipeline artefacts produced from a fact-extraction batch.

    - ``vector_items`` — one VectorItem per fact, ready for store_vectors.
      The item's ``id`` is also the fact's identifier for causal/entity
      linkage. Order matches the input ``ExtractedFact`` list.
    - ``entities`` — deduplicated Entity list across the batch (one
      entity per unique (name, type) tuple). Each entity uses a
      deterministic ID ``{type}:{slug}`` so subsequent retain calls
      collapse repeated mentions to the same canonical row.
    - ``memory_entity_associations`` — list of (vector_item_id,
      entity_id) tuples for the orchestrator's
      link_memories_to_entities call.
    - ``memory_links`` — directional caused_by edges; source is the
      effect's memory ID, target is the cause's memory ID.
    """

    vector_items: list[VectorItem]
    entities: list[Entity]
    memory_entity_associations: list[tuple[str, str]]
    memory_links: list[MemoryLink]


def materialize_facts(
    facts: list[ExtractedFact],
    *,
    bank_id: str,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    occurred_at: datetime | None = None,
    embeddings: list[list[float]] | None = None,
    verbatim: bool = False,
) -> MaterializedFacts:
    """Convert extracted facts to retain-pipeline artefacts.

    The orchestrator wires this output into its existing storage path
    (store_vectors → store_entities → link_memories_to_entities →
    store_memory_links) without further translation.

    Args:
        facts: Output of :func:`extract_facts` or :func:`extract_facts_verbatim`.
        bank_id: Target bank.
        tags: Tags applied to every produced VectorItem.
        metadata: Base metadata merged onto every VectorItem; the
            fact's structured fields (when/where/who/why/fact_type)
            are written under ``_fact_*`` prefixed keys for downstream
            queries that want to filter/promote on them.
        occurred_at: Default timestamp when a fact lacks
            ``occurred_start``. Typically the retain request's
            ``occurred_at``.
        embeddings: Optional pre-computed embeddings per fact (must
            match ``len(facts)``). When omitted, items are created
            with empty vectors — caller is responsible for embedding.
        verbatim: When True, the VectorItem's text is the raw chunk
            text (``fact.what``) — no Involving/At/When decorations.
            Use with :func:`extract_facts_verbatim` to preserve
            original vocabulary for embedding-match against questions.
            When False (default), uses :meth:`ExtractedFact.build_text`
            which decorates the fact with structured field annotations.
    """
    base_metadata = dict(metadata or {})
    base_tags = list(tags or [])
    now = datetime.now(timezone.utc)

    # Step 1: deduplicate entities across the batch by (name, type).
    # Entity IDs are deterministic — same name+type produces same ID.
    entity_by_key: dict[tuple[str, str], Entity] = {}
    for fact in facts:
        for fent in fact.entities:
            key = (fent.name.strip().lower(), fent.entity_type.strip().upper())
            if key in entity_by_key:
                continue
            eid = f"{key[1].lower()}:{_slug(fent.name)}"
            entity_by_key[key] = Entity(
                id=eid,
                name=fent.name,
                entity_type=fent.entity_type,
                aliases=[fent.name],
                metadata={"source": "fact_extraction"},
            )
    entities = list(entity_by_key.values())

    # Step 2: build VectorItems, one per fact. The IDs flow through
    # causal_relations resolution below.
    items: list[VectorItem] = []
    associations: list[tuple[str, str]] = []
    for idx, fact in enumerate(facts):
        item_id = uuid.uuid4().hex
        fact_metadata = dict(base_metadata)
        # Promote structured dimensions into metadata so downstream
        # consumers can filter/rerank on them. Use the ``_fact_*``
        # prefix to avoid colliding with caller-supplied keys.
        if fact.when and fact.when.upper() != "N/A":
            fact_metadata["_fact_when"] = fact.when
        if fact.where and fact.where.upper() != "N/A":
            fact_metadata["_fact_where"] = fact.where
        if fact.who and fact.who.upper() != "N/A":
            fact_metadata["_fact_who"] = fact.who
        if fact.why and fact.why.upper() != "N/A":
            fact_metadata["_fact_why"] = fact.why
        fact_metadata["_fact_type"] = fact.fact_type
        if fact.occurred_start is not None:
            fact_metadata["_fact_occurred_start"] = fact.occurred_start.isoformat()
        if fact.occurred_end is not None:
            fact_metadata["_fact_occurred_end"] = fact.occurred_end.isoformat()

        vector = embeddings[idx] if embeddings is not None and idx < len(embeddings) else []

        items.append(
            VectorItem(
                id=item_id,
                bank_id=bank_id,
                vector=vector,
                # Verbatim mode: store the chunk text as-is so question
                # embeddings can match against the original vocabulary.
                # Concise mode: use the structured/decorated fact text.
                text=fact.what if verbatim else fact.build_text(),
                metadata=fact_metadata,
                tags=list(base_tags),
                fact_type=fact.fact_type,
                memory_layer="raw",
                occurred_at=fact.occurred_start or occurred_at,
                retained_at=now,
            )
        )

        # Association: link this memory to each entity it mentions.
        for fent in fact.entities:
            key = (fent.name.strip().lower(), fent.entity_type.strip().upper())
            ent = entity_by_key.get(key)
            if ent is not None:
                associations.append((item_id, ent.id))

    # Step 3: causal relations → MemoryLinks. Source is the EFFECT
    # (the fact making the claim); target is the CAUSE.
    memory_links: list[MemoryLink] = []
    for src_idx, fact in enumerate(facts):
        for rel in fact.causal_relations:
            tgt_idx = rel.target_fact_index
            if tgt_idx == src_idx:
                continue  # self-loop
            if not (0 <= tgt_idx < len(facts)):
                continue
            try:
                strength = float(rel.strength)
            except (TypeError, ValueError):
                strength = 1.0
            memory_links.append(
                MemoryLink(
                    source_memory_id=items[src_idx].id,
                    target_memory_id=items[tgt_idx].id,
                    link_type="caused_by",
                    confidence=min(1.0, max(0.0, strength)),
                    weight=1.0,
                    created_at=now,
                    metadata={"bank_id": bank_id, "source": "fact_extraction"},
                )
            )

    return MaterializedFacts(
        vector_items=items,
        entities=entities,
        memory_entity_associations=associations,
        memory_links=memory_links,
    )
