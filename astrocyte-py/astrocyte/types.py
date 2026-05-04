"""Astrocyte core types — all DTOs for the framework.

Every type here is FFI-safe: no Any, no callables, no generators.
Fields use only: str, int, float, bool, None, list, dict, datetime, dataclass.
See docs/_design/implementation-language-strategy.md for constraints.
"""

from __future__ import annotations

import json as _json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Literal

from astrocyte.mip.schema import ForgetSpec, PipelineSpec

# ---------------------------------------------------------------------------
# Metadata value type — recursive union replacing Any for FFI safety
# ---------------------------------------------------------------------------
MetadataValue = str | int | float | bool | None
Metadata = dict[str, MetadataValue]

# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------


@dataclass
class HealthStatus:
    healthy: bool
    message: str | None = None
    latency_ms: float | None = None
    last_check_at: datetime | None = None


# ---------------------------------------------------------------------------
# Tier 1: Vector Store
# ---------------------------------------------------------------------------


@dataclass
class VectorItem:
    id: str
    bank_id: str
    vector: list[float]
    text: str
    metadata: Metadata | None = None
    tags: list[str] | None = None
    fact_type: str | None = None  # "world", "experience", "observation"
    occurred_at: datetime | None = None
    memory_layer: str | None = None  # "fact", "observation", "model" — memory hierarchy
    retained_at: datetime | None = None  # UTC wall-clock when this item was stored (M9)
    #: Optional backreference to the originating ``SourceChunk.id`` (M10).
    #: When set, the vector store persists it onto ``astrocyte_vectors.chunk_id``
    #: so recall can resolve provenance (`document_id`, `source_uri`) and
    #: trigger chunk-level expansion. Nullable for backward compat — vectors
    #: ingested without a SourceStore retain stamp ``None``.
    chunk_id: str | None = None

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("VectorItem.text must be non-empty")


@dataclass
class VectorFilters:
    bank_id: str | None = None
    tags: list[str] | None = None
    fact_types: list[str] | None = None
    time_range: tuple[datetime, datetime] | None = None
    metadata_filters: Metadata | None = None
    as_of: datetime | None = None  # Time-travel: only return items retained on or before this timestamp (M9)


@dataclass
class VectorHit:
    id: str
    text: str
    score: float  # 0.0 – 1.0 similarity
    metadata: Metadata | None = None
    tags: list[str] | None = None
    fact_type: str | None = None
    occurred_at: datetime | None = None
    memory_layer: str | None = None  # "fact", "observation", "model"
    retained_at: datetime | None = None  # UTC timestamp when item was retained (M9)
    #: M10: backreference to the originating ``SourceChunk.id``. When set,
    #: callers can resolve provenance via ``SourceStore.get_chunk(chunk_id)
    #: → SourceStore.get_document(chunk.document_id)`` to surface citations.
    #: ``None`` for legacy vectors retained without a SourceStore.
    chunk_id: str | None = None

    def __post_init__(self) -> None:
        if self.score < 0.0:
            raise ValueError(f"VectorHit.score must be >= 0.0, got {self.score}")


# ---------------------------------------------------------------------------
# Tier 1: Graph Store
# ---------------------------------------------------------------------------


@dataclass
class Entity:
    id: str
    name: str
    entity_type: str  # PERSON, ORG, LOCATION, …
    aliases: list[str] | None = None
    metadata: Metadata | None = None
    #: Per-entity name embedding for the Hindsight-inspired entity-resolution
    #: cascade. When present, the resolver uses cosine similarity against
    #: candidate embeddings to decide whether two surface forms refer to the
    #: same canonical entity, falling back to LLM disambiguation only for
    #: genuinely ambiguous pairs. Optional and nullable — a ``None`` value
    #: signals "no embedding tier available for this entity" and the cascade
    #: degrades gracefully to trigram + LLM.
    embedding: list[float] | None = None
    #: Hindsight-parity mention count — how many times this entity has been
    #: resolved-to during retain. Treated as a soft popularity signal in the
    #: composite cascade (cheap tiebreaker, never a primary decider). Adapter
    #: stores increment this on every successful canonical resolution; new
    #: entities start at 1.
    mention_count: int = 1


@dataclass
class EntityCandidateMatch:
    """Scored candidate produced by ``GraphStore.find_entity_candidates_scored``.

    The Hindsight-inspired entity-resolution cascade asks the graph store for
    candidates pre-scored against four cheap signals — trigram, embedding,
    co-occurrence, and temporal proximity — so the resolver can decide
    whether to autolink, skip, or escalate to the LLM without paying for
    additional database round-trips.

    Fields:
        entity: The candidate entity.
        name_similarity: Trigram similarity of the candidate's name against
            the query name in ``[0.0, 1.0]``. Adapters compute this with
            ``pg_trgm.similarity()`` (PostgreSQL) or ``difflib.SequenceMatcher``
            (in-memory).
        embedding_similarity: Cosine similarity of the candidate's stored
            embedding against the supplied query embedding in ``[0.0, 1.0]``,
            or ``None`` when either side has no embedding stored.
        co_occurring_names: Names of entities that co-occur with this
            candidate via ``EntityLink(link_type="co_occurs")``. Lowercased.
            Used by the resolver to compute overlap with the new entity's
            nearby entities — strong evidence two surface forms refer to
            the same canonical entity when their contexts overlap.
        last_seen: The candidate's last-activity timestamp (typically
            ``updated_at``). Used to compute temporal proximity to the new
            entity's event date — recent candidates score higher for the
            same name. ``None`` when not stored.
    """

    entity: Entity
    name_similarity: float
    embedding_similarity: float | None = None
    co_occurring_names: list[str] = field(default_factory=list)
    last_seen: datetime | None = None
    #: Hindsight-parity popularity signal — how many memories this entity has
    #: been resolved-to. Used by :meth:`EntityResolver._composite_score` as a
    #: soft tiebreaker (capped contribution; never overrides name/cooccurrence
    #: signals on its own).
    mention_count: int = 1


@dataclass
class EntityLink:
    """A typed relationship between two entities in the knowledge graph.

    M11: fields renamed from ``source_entity_id``/``target_entity_id`` to
    ``entity_a``/``entity_b`` to be direction-neutral; ``evidence``,
    ``confidence``, and ``created_at`` added for entity resolution provenance.
    """

    entity_a: str
    """ID of the first entity in the relationship."""

    entity_b: str
    """ID of the second entity in the relationship."""

    link_type: str
    """Relationship label — e.g. ``"alias_of"``, ``"co_occurs"``, ``"works_at"``."""

    evidence: str = ""
    """Verbatim quote from the source memory that justifies this link."""

    confidence: float = 1.0
    """0–1 confidence score. 1.0 = rule-derived; < 1.0 = LLM-confirmed."""

    created_at: datetime | None = None
    """UTC wall-clock time this link was created. None for legacy links."""

    metadata: Metadata | None = None
    """Optional extra key-value pairs (preserved for backward compatibility)."""


@dataclass
class MemoryEntityAssociation:
    memory_id: str
    entity_id: str


@dataclass
class MemoryLink:
    """A typed directional link between two memories (Hindsight parity).

    Distinct from :class:`EntityLink` (which connects entities). Memory
    links capture relationships between fact-level units — the granularity
    Hindsight's ``link_expansion_retrieval`` walks for causal chains and
    semantic-kNN edges.

    Three link types are first-class in the link-expansion retrieval
    signal:
    - ``"caused_by"`` — extracted at retain time from cause-effect text
      ("she lost her job, so she couldn't pay rent").
    - ``"semantic"`` — precomputed kNN (each new memory linked to its
      top-K most similar prior memories at insert time, similarity ≥ 0.7).
    - ``"entity_overlap"`` — query-time signal computed from shared
      entities (not persisted; included here for documentation parity).

    Direction matters: ``source_memory_id`` is the "from" side. For
    ``caused_by`` semantics, the source is the EFFECT and the target is
    the CAUSE. (Hindsight's convention; preserved here for parity.)
    """

    source_memory_id: str
    target_memory_id: str
    link_type: str
    evidence: str = ""
    confidence: float = 1.0
    weight: float = 1.0
    created_at: datetime | None = None
    metadata: Metadata | None = None


@dataclass
class GraphHit:
    memory_id: str
    text: str
    connected_entities: list[str]
    depth: int
    score: float


# ---------------------------------------------------------------------------
# Tier 1: Document Store
# ---------------------------------------------------------------------------


@dataclass
class Document:
    id: str
    text: str
    metadata: Metadata | None = None
    tags: list[str] | None = None


@dataclass
class DocumentFilters:
    tags: list[str] | None = None
    metadata_filters: Metadata | None = None


@dataclass
class DocumentHit:
    document_id: str
    text: str
    score: float  # BM25 relevance
    metadata: Metadata | None = None


# ---------------------------------------------------------------------------
# Tier 2: Engine Provider — requests / results
# ---------------------------------------------------------------------------


@dataclass
class RetainRequest:
    content: str
    bank_id: str
    metadata: Metadata | None = None
    tags: list[str] | None = None
    occurred_at: datetime | None = None
    source: str | None = None
    content_type: str = "text"  # "text", "conversation", "document", "email", ...
    extraction_profile: str | None = None  # key in astrocyte.yml extraction_profiles (M3)
    #: Optional pipeline overrides from a MIP RoutingDecision. When set, fields
    #: take precedence over extraction profile and content_type defaults during
    #: chunking and dedup. Persisted onto each stored chunk via ``_mip.*`` keys.
    mip_pipeline: PipelineSpec | None = None
    #: Name of the MIP rule whose action produced ``mip_pipeline``. Persisted on
    #: stored chunks as ``_mip.rule`` so recall can warn on rule-version drift.
    mip_rule_name: str | None = None


@dataclass
class RetainResult:
    stored: bool
    memory_id: str | None = None
    deduplicated: bool = False
    error: str | None = None
    retention_action: str | None = None  # "add" | "update" | "merge" | "skip" | "delete" (curated retain)
    curated: bool = False  # Whether LLM curation was used
    memory_layer: str | None = None  # Layer assigned during curation


@dataclass
class RecallRequest:
    query: str
    bank_id: str
    max_results: int = 10
    max_tokens: int | None = None
    fact_types: list[str] | None = None
    tags: list[str] | None = None
    time_range: tuple[datetime, datetime] | None = None
    include_sources: bool = False
    layer_weights: dict[str, float] | None = None  # {"fact": 1.0, "observation": 1.5, "model": 2.0}
    detail_level: str | None = None  # "titles" | "bodies" | "full" | None (default=full)
    external_context: list[MemoryHit] | None = None  # External RAG/graph results for cross-source fusion
    as_of: datetime | None = None  # Time-travel: recall as if it were this UTC moment (M9)


@dataclass
class MemoryHit:
    text: str
    score: float  # 0.0 – 1.0 relevance
    fact_type: str | None = None
    metadata: Metadata | None = None
    tags: list[str] | None = None
    occurred_at: datetime | None = None
    source: str | None = None
    memory_id: str | None = None
    bank_id: str | None = None  # set by multi-bank / hybrid recall
    memory_layer: str | None = None  # "fact", "observation", "model"
    utility_score: float | None = None  # 0.0 – 1.0 composite utility
    retained_at: datetime | None = None  # UTC timestamp when item was retained (M9)
    chunk_id: str | None = None  # M10: source-chunk backreference (when source_store is wired)


@dataclass
class RecallTrace:
    strategies_used: list[str] | None = None
    total_candidates: int | None = None
    fusion_method: str | None = None
    latency_ms: float | None = None
    strategy_timings_ms: dict[str, float] | None = None
    strategy_candidate_counts: dict[str, int] | None = None
    tier_used: int | None = None  # Which retrieval tier resolved the query
    layer_distribution: dict[str, int] | None = None  # {"fact": 5, "observation": 3, "model": 1}
    cache_hit: bool | None = None  # Whether recall cache was used
    wiki_tier_used: bool | None = None  # True when wiki tier satisfied the query (M8 W5)


@dataclass
class RecallResult:
    hits: list[MemoryHit]
    total_available: int
    truncated: bool
    trace: RecallTrace | None = None
    #: Optional labeled sections + rules for synthesis (M7 structured recall authority).
    authority_context: str | None = None
    #: Top raw cosine-similarity score from the semantic strategy (0.0 when no semantic
    #: results were found).  Used by the reflect evidence-strict gate to detect uncertain
    #: retrieval and force citation rather than letting the LLM hallucinate from
    #: tangential memories.
    top_semantic_score: float = 0.0


@dataclass
class HistoryResult:
    """Result of ``brain.history()`` — what the agent knew at a past point in time (M9).

    Wraps a :class:`RecallResult` and carries the ``as_of`` timestamp so
    callers can log/display the reconstruction point without parsing the request.
    """

    hits: list[MemoryHit]
    total_available: int
    truncated: bool
    as_of: datetime  # The UTC timestamp used for the time-travel query
    bank_id: str
    trace: RecallTrace | None = None


@dataclass
class GapItem:
    """A single knowledge gap identified by ``brain.audit()`` (M10).

    A gap is a topic or question that the memory bank cannot answer
    adequately — either because no memories cover it, or because coverage
    is too thin to draw a reliable conclusion.
    """

    topic: str
    """Short label for the missing or under-covered topic (e.g. ``"Alice's current role"``)."""

    severity: Literal["high", "medium", "low"]
    """How critical the gap is.

    - ``"high"`` — likely to cause a wrong or confidently-wrong answer.
    - ``"medium"`` — partial coverage; answer may be incomplete.
    - ``"low"`` — minor; nuance or context is missing.
    """

    reason: str
    """One-sentence explanation of why the gap exists."""


@dataclass
class AuditResult:
    """Result of ``brain.audit()`` — structured gap analysis for a scope (M10).

    Summarises what the agent *doesn't* know about a given topic, together
    with a 0–1 coverage score and provenance counts.
    """

    scope: str
    """The scope string passed to ``brain.audit()``."""

    bank_id: str
    """The bank that was audited."""

    gaps: list[GapItem]
    """Identified knowledge gaps, ordered roughly by severity."""

    coverage_score: float
    """0–1 composite score (memory density × recency × topic breadth).

    1.0 means the bank covers the scope well; < 0.5 indicates sparse coverage.
    """

    memories_scanned: int
    """Number of memories retrieved and fed to the audit judge."""

    trace: RecallTrace | None = None
    """Diagnostic trace from the recall pass, if available."""


@dataclass
class Dispositions:
    """Personality modifiers for synthesis."""

    skepticism: int = 3  # 1 (trusting) to 5 (skeptical)
    literalism: int = 3  # 1 (flexible) to 5 (rigid)
    empathy: int = 3  # 1 (detached) to 5 (empathetic)

    def __post_init__(self) -> None:
        for field_name in ("skepticism", "literalism", "empathy"):
            val = getattr(self, field_name)
            if not (1 <= val <= 5):
                raise ValueError(f"Dispositions.{field_name} must be 1–5, got {val}")


@dataclass
class ReflectRequest:
    query: str
    bank_id: str
    max_tokens: int | None = None
    include_sources: bool = True
    dispositions: Dispositions | None = None
    #: Optional tag filter forwarded to the dispatcher's internal recall.
    #: When set, reflect's underlying retrieval is scoped to memories
    #: carrying every listed tag — closing the leak where single-bank
    #: ``Astrocyte.reflect(tags=...)`` previously dropped the filter.
    tags: list[str] | None = None


@dataclass
class ReflectResult:
    answer: str
    confidence: float | None = None
    sources: list[MemoryHit] | None = None
    observations: list[str] | None = None
    #: Same structured block as :attr:`RecallResult.authority_context` when reflect used recall authority.
    authority_context: str | None = None


@dataclass
class ForgetRequest:
    bank_id: str
    memory_ids: list[str] | None = None
    tags: list[str] | None = None
    before_date: datetime | None = None
    scope: str | None = None  # "all" or None for selective


@dataclass
class ForgetResult:
    deleted_count: int
    archived_count: int = 0


# ---------------------------------------------------------------------------
# Engine capabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineCapabilities:
    supports_reflect: bool = False
    supports_forget: bool = False
    supports_semantic_search: bool = True
    supports_keyword_search: bool = False
    supports_graph_search: bool = False
    supports_temporal_search: bool = False
    supports_dispositions: bool = False
    supports_consolidation: bool = False
    supports_entities: bool = False
    supports_tags: bool = False
    supports_metadata: bool = True
    supports_compile: bool = False  # M8: wiki compile via brain.compile()
    max_retain_bytes: int | None = None
    max_recall_results: int | None = None
    max_embedding_dims: int | None = None


# ---------------------------------------------------------------------------
# LLM Provider
# ---------------------------------------------------------------------------


@dataclass
class ContentPart:
    """Tagged union for multimodal content."""

    type: str  # "text", "image_url", "image_base64", "audio_url", "audio_base64"
    text: str | None = None
    image_url: str | None = None
    image_base64: str | None = None
    audio_url: str | None = None
    audio_base64: str | None = None
    media_type: str | None = None  # MIME type for base64 content, e.g. "image/png", "image/jpeg"


@dataclass
class Message:
    role: str  # "system", "user", "assistant", "tool"
    content: str | list[ContentPart] = ""
    #: When ``role == "assistant"`` and the model emitted tool calls, the
    #: provider adapter places them here so downstream consumers can
    #: round-trip them back into a follow-up turn (along with the
    #: matching ``role="tool"`` results).
    tool_calls: list[ToolCall] | None = None
    #: When ``role == "tool"``, the OpenAI / Anthropic spec requires this
    #: field to point back at the originating ``ToolCall.id``. The provider
    #: adapter uses it to reconstruct the wire-format tool result message.
    tool_call_id: str | None = None
    #: When ``role == "tool"``, the human-readable tool name. Carried for
    #: providers (and for our own logging) that surface the name on
    #: tool result messages.
    name: str | None = None


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int


#: JSON-shaped value type for tool-call arguments and JSON Schema —
#: replaces ``Any`` to satisfy the FFI-safety constraint on DTOs
#: (see :data:`Metadata` for the same idiom on memory metadata).
JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass
class ToolCall:
    """A single tool invocation requested by an LLM during native function calling.

    Mirrors the OpenAI/Anthropic tool-call shape: each call carries an
    opaque ``id`` (for round-tripping a tool result back to the model),
    a ``name`` matching one of the tools provided in the request, and
    the model's chosen ``arguments`` as a parsed dict.

    The agentic reflect loop (Hindsight parity) consumes these instead
    of parsing JSON out of the response text — significantly more
    reliable than the JSON-in-prose protocol.
    """

    id: str
    name: str
    arguments: dict[str, JsonValue]


@dataclass
class ToolDefinition:
    """A tool the LLM can call. JSON-Schema-shaped, OpenAI-compatible.

    ``parameters`` is a JSON Schema object; the provider adapter is
    responsible for translating to the wire format the underlying API
    expects (OpenAI: ``tools=[{"type": "function", "function": {...}}]``;
    Anthropic: ``tools=[{"name": ..., "input_schema": ...}]``).
    """

    name: str
    description: str
    parameters: dict[str, JsonValue]


@dataclass
class Completion:
    text: str
    model: str
    usage: TokenUsage | None = None
    #: Tool calls the LLM emitted, when ``tools`` were supplied to
    #: :meth:`LLMProvider.complete`. ``None`` means the provider did
    #: not produce tool calls (or doesn't support them — feature-detect
    #: with ``getattr`` rather than assuming).
    tool_calls: list[ToolCall] | None = None


@dataclass(frozen=True)
class LLMCapabilities:
    supports_multimodal_completion: bool = False
    modalities_supported: tuple[str, ...] | None = None
    supports_multimodal_embedding: bool = False
    supports_batch_embed: bool = True


# ---------------------------------------------------------------------------
# Outbound Transport
# ---------------------------------------------------------------------------


@dataclass
class HttpClientContext:
    proxy: str | None = None
    ca_bundle: str | None = None
    headers: dict[str, str] | None = None
    timeouts: dict[str, float] | None = None


@dataclass(frozen=True)
class TransportCapabilities:
    supports_proxy: bool = False
    supports_custom_ca: bool = False
    supports_client_cert: bool = False
    supports_headers: bool = False


# ---------------------------------------------------------------------------
# Multi-bank orchestration
# ---------------------------------------------------------------------------


@dataclass
class MultiBankStrategy:
    """Multi-bank recall behavior. Default ``parallel`` matches legacy ``banks=[...]`` without an explicit strategy."""

    mode: Literal["cascade", "parallel", "first_match"] = "parallel"
    min_results_to_stop: int = 3
    cascade_order: list[str] | None = None
    bank_weights: dict[str, float] | None = None
    dedup_across_banks: bool = True


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


_VALID_PERMISSIONS = {"read", "write", "forget", "admin", "*"}


@dataclass
class AccessGrant:
    bank_id: str  # or "*"
    principal: str  # or "*"
    permissions: list[str]  # ["read", "write", "forget", "admin"]

    def __post_init__(self) -> None:
        invalid = set(self.permissions) - _VALID_PERMISSIONS
        if invalid:
            raise ValueError(f"AccessGrant.permissions contains invalid values: {invalid}")


@dataclass
class ActorIdentity:
    """Structured actor for access control and bank resolution (ADR-002)."""

    type: str  # "user" | "agent" | "service"
    id: str
    claims: dict[str, str] | None = None


@dataclass
class AstrocyteContext:
    """Caller identity for access control.

    ``principal`` remains the backwards-compatible primary string. When ``actor``
    is set, identity resolution uses ``actor`` (and optional ``on_behalf_of`` for OBO);
    ``principal`` is still useful for logging and integrations that have not migrated.
    """

    principal: str  # e.g. "agent:support-bot-1", "user:calvin"
    actor: ActorIdentity | None = None
    on_behalf_of: ActorIdentity | None = None
    tenant_id: str | None = None


# ---------------------------------------------------------------------------
# Event hooks
# ---------------------------------------------------------------------------


@dataclass
class HookEvent:
    event_id: str
    type: str  # e.g. "on_retain", "on_pii_detected"
    timestamp: datetime
    bank_id: str | None = None
    data: Metadata | None = None
    trace_id: str | None = None


# ---------------------------------------------------------------------------
# Data governance
# ---------------------------------------------------------------------------


@dataclass
class DataClassification:
    level: int  # 0-3
    label: str  # "public", "internal", "confidential", "restricted"
    categories: list[str] | None = None  # ["PII", "PHI", …] for restricted
    classified_by: str = "rules"  # "caller", "rules", "llm"
    classified_at: datetime | None = None


# ---------------------------------------------------------------------------
# Lifecycle / audit
# ---------------------------------------------------------------------------


@dataclass
class LegalHold:
    hold_id: str
    bank_id: str
    reason: str
    set_at: datetime
    set_by: str  # "user:api", "system:compliance"


@dataclass
class LifecycleAction:
    """Result of a lifecycle TTL evaluation on a single memory."""

    memory_id: str
    action: str  # "archive" | "delete" | "keep"
    reason: str  # "ttl_unretrieved" | "ttl_archived_expired" | "recent" | "exempt" | "legal_hold"


@dataclass
class LifecycleRunResult:
    archived_count: int
    deleted_count: int
    skipped_count: int
    actions: list[LifecycleAction]


@dataclass
class AuditEvent:
    event_type: str
    bank_id: str
    actor: str  # "system:ttl", "user:api", "compliance:forget", …
    timestamp: datetime
    memory_ids: list[str] | None = None
    reason: str | None = None
    metadata: Metadata | None = None


@dataclass
class ForgetSelector:
    bank_ids: list[str]
    scope: str | None = None  # "all" or None for selective
    tags: list[str] | None = None
    before_date: datetime | None = None
    memory_ids: list[str] | None = None


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@dataclass
class HealthIssue:
    severity: Literal["info", "warning", "critical"]
    code: str
    message: str
    recommendation: str


@dataclass
class BankHealth:
    bank_id: str
    score: float  # 0.0 – 1.0
    status: Literal["healthy", "warning", "unhealthy"]
    issues: list[HealthIssue]
    metrics: dict[str, float]
    assessed_at: datetime


@dataclass
class MemoryUsage:
    memory_id: str
    text: str
    recall_count: int
    last_recalled_at: datetime


@dataclass
class QualityDataPoint:
    date: date
    retain_count: int
    recall_count: int
    recall_hit_rate: float
    avg_recall_score: float
    dedup_rate: float
    reflect_success_rate: float


@dataclass
class UtilizationReport:
    bank_id: str
    total_memories: int
    active_memories: int  # recalled >= 1x in last 30 days
    stale_memories: int  # never recalled in 30 days
    never_recalled: int
    top_recalled: list[MemoryUsage]
    fact_type_distribution: dict[str, int]
    tag_distribution: dict[str, int]
    assessed_at: datetime


@dataclass
class QualityTrends:
    bank_id: str
    data_points: list[QualityDataPoint]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class EvalMetrics:
    recall_precision: float
    recall_hit_rate: float
    recall_mrr: float
    recall_ndcg: float
    retain_latency_p50_ms: float
    retain_latency_p95_ms: float
    recall_latency_p50_ms: float
    recall_latency_p95_ms: float
    total_tokens_used: int
    total_duration_seconds: float
    reflect_accuracy: float | None = None
    reflect_completeness: float | None = None
    reflect_hallucination_rate: float | None = None
    reflect_latency_p50_ms: float | None = None
    reflect_latency_p95_ms: float | None = None
    # Tier-3 metrics: populated by ``BenchmarkMetricsCollector`` when the
    # bench wires it up. All optional so legacy callers stay valid.

    # ── Quality ──────────────────────────────────────────────
    #: Fraction of questions where at least one retrieved memory had
    #: token-overlap ≥ threshold with the gold answer. Distinguishes
    #: "missed evidence" from "had evidence, synthesised wrong".
    recall_coverage: float | None = None
    #: Cross-tab of (recall_hit, reflect_hit) outcomes. Keys:
    #: ``"both_hit"``, ``"recall_hit_reflect_miss"``,
    #: ``"recall_miss_reflect_hit"``, ``"both_miss"``.
    recall_reflect_gap: dict[str, int] | None = None
    #: For adversarial questions: fraction where reflect correctly abstained
    #: ("Insufficient evidence" / "I don't have …" patterns) instead of
    #: hallucinating an answer.
    abstention_rate_adversarial: float | None = None

    # ── Cost & efficiency ────────────────────────────────────
    #: Tokens by pipeline phase. Keys: ``"retain"``, ``"eval"``,
    #: ``"persona_compile"``, ``"observation_consolidation"``, ``"other"``.
    tokens_by_phase: dict[str, int] | None = None
    #: Total HTTP API calls across the run.
    api_calls_total: int | None = None
    #: HTTP API calls by endpoint. Typical keys: ``"chat/completions"``,
    #: ``"embeddings"``.
    api_calls_by_endpoint: dict[str, int] | None = None
    #: Total cost in USD computed from per-model pricing × actual tokens.
    cost_total_usd: float | None = None
    #: Mean cost per question.
    cost_per_question_usd: float | None = None

    # ── Latency (tail) ───────────────────────────────────────
    retain_latency_p99_ms: float | None = None
    recall_latency_p99_ms: float | None = None
    reflect_latency_p99_ms: float | None = None
    #: Median end-to-end question time (recall + reflect).
    e2e_per_question_p50_ms: float | None = None
    e2e_per_question_p95_ms: float | None = None

    # ── Robustness ───────────────────────────────────────────
    #: Counts of categorised errors. Typical keys: ``"pool_timeout"``,
    #: ``"deadlock"``, ``"openai_429"``, ``"openai_5xx"``, ``"other"``.
    error_count_by_type: dict[str, int] | None = None
    #: Number of OpenAI retries triggered (rate limits, transient errors).
    openai_retry_count: int | None = None
    #: Number of retain calls that returned ``stored=False`` or raised.
    failed_retain_count: int | None = None

    # ── Memory-architecture (cascade observability) ──────────
    #: Counts of entity-resolution cascade decisions. Typical keys:
    #: ``"trigram_autolink"``, ``"embedding_autolink"``,
    #: ``"composite_autolink"``, ``"llm_disambiguation"``, ``"skipped"``,
    #: ``"created_new"``.
    cascade_decisions: dict[str, int] | None = None
    #: Number of new entities resolved to an existing canonical via Path B
    #: (count of pre-store ID rewrites).
    entities_resolved_count: int | None = None
    #: Number of new entities that became fresh canonicals (no match).
    entities_created_count: int | None = None
    #: Histogram of composite scores keyed by bucket label
    #: (``"0.0-0.1"``, ``"0.1-0.2"``, …, ``"0.9-1.0"``).
    composite_score_distribution: dict[str, int] | None = None
    #: Total wiki-page rows in the bank at end-of-run.
    wiki_pages_total: int | None = None
    #: Mean number of wiki pages per unique persona name. >1 indicates
    #: scoping (per-conversation pages); 1 indicates single canonical.
    wiki_pages_per_persona: float | None = None


@dataclass
class QueryResult:
    query: str
    expected: list[str]
    actual: list[MemoryHit]
    relevant_found: int
    precision: float
    reciprocal_rank: float
    latency_ms: float


@dataclass
class EvalResult:
    suite: str
    provider: str
    provider_tier: str
    timestamp: datetime
    metrics: EvalMetrics
    per_query_results: list[QueryResult]
    config_snapshot: Metadata | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-safe dict (datetime → ISO 8601 string)."""

        def _convert(obj: object) -> object:
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            return obj

        raw = asdict(self)
        return _json.loads(_json.dumps(raw, default=_convert))

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize to a JSON string."""
        return _json.dumps(self.to_dict(), indent=indent)


@dataclass
class RegressionAlert:
    metric: str
    current_value: float
    baseline_value: float
    delta: float
    delta_percent: float
    severity: Literal["warning", "critical"]


# ---------------------------------------------------------------------------
# MIP routing
# ---------------------------------------------------------------------------


@dataclass
class RoutingDecision:
    """Output of MIP routing — tells Astrocyte where/how to store."""

    bank_id: str | None = None
    tags: list[str] | None = None
    retain_policy: str | None = None  # "default" | "redact_before_store" | "encrypt" | "reject"
    resolved_by: str = "passthrough"  # "mechanical" | "intent" | "passthrough"
    rule_name: str | None = None
    confidence: float = 1.0
    reasoning: str | None = None  # LLM justification if intent layer used
    pipeline: PipelineSpec | None = None  # Optional pipeline-shaping overrides from rule
    forget: ForgetSpec | None = None  # Optional forget-policy overrides from rule (Phase 4)
    observability_tags: list[str] | None = None  # Per-rule operator labels (Phase 5)


# ---------------------------------------------------------------------------
# M8: Wiki Compile
# ---------------------------------------------------------------------------

WikiPageKind = Literal["entity", "topic", "concept"]


@dataclass
class WikiPage:
    """A compiled topic/entity/concept page synthesised from raw memories (M8).

    WikiPages are additive artefacts — raw memories are never removed when a
    page is compiled. Each page carries ``source_ids`` back to every raw memory
    that contributed, enabling provenance tracing and recompile-on-forget.

    Pages are mutable: each compile pass produces a new revision. Past revisions
    are kept in the WikiStore audit log (not indexed for recall).
    """

    page_id: str  # Stable ID, e.g. "topic:incident-response", "entity:alice"
    bank_id: str
    kind: WikiPageKind  # "entity" | "topic" | "concept"
    title: str
    content: str  # LLM-maintained markdown
    scope: str  # Scope string used for this compile (tag name or cluster label)
    source_ids: list[str]  # Raw memory IDs that contributed (provenance)
    cross_links: list[str]  # Other page_ids referenced in this page
    revision: int  # Monotonically increasing, starts at 1
    revised_at: datetime
    tags: list[str] | None = None  # Inherited from contributing memories
    metadata: Metadata | None = None


@dataclass
class WikiPageHit:
    """A wiki page returned from a semantic search during recall tiering."""

    page_id: str
    title: str
    content: str
    scope: str
    kind: str
    score: float  # 0.0 – 1.0 similarity
    source_ids: list[str]
    bank_id: str


@dataclass(frozen=True)
class MentalModel:
    """A first-class curated saved-reflect summary.

    Mental models are durable, refreshable artifacts — the "Caroline
    prefers async updates" / "Project X status: blocked on review" rows
    that outlive any single recall and serve as authoritative summaries
    when the recall pipeline elects to use the compiled layer.

    Stored in the dedicated :class:`~astrocyte.provider.MentalModelStore`
    SPI (formerly piggybacked on :class:`WikiStore` with
    ``kind="concept"`` + ``metadata["_mental_model"] = True``; that
    discriminator pattern was an architecture smell that we cut to a
    proper table in v1.x — see ``docs/_plugins/benchmark-presets.md``).

    Attributes:
        model_id: Stable identifier within the bank (e.g.
            ``"model:alice-prefs"``).
        bank_id: Tenant-scoped bank identifier.
        title: Human-readable display title.
        content: The summary body, typically markdown.
        scope: Scope key — ``"bank"`` for bank-wide models, or a
            specific tag like ``"person:alice"`` to scope to a topic.
        source_ids: Raw memory IDs that contributed to this summary
            (provenance — enables refresh-on-forget).
        revision: Monotonically increasing version number, starts at 1.
            Bumped by ``MentalModelStore.upsert`` on each refresh.
        refreshed_at: Timestamp of the most recent refresh.
    """

    model_id: str
    bank_id: str
    title: str
    content: str
    scope: str
    source_ids: list[str]
    revision: int
    refreshed_at: datetime


# ---------------------------------------------------------------------------
# Source documents and chunks (M10 — provenance + dedup)
# ---------------------------------------------------------------------------
#
# Three-layer hierarchy: SourceDocument → SourceChunk → VectorItem.
#
# Pre-M10, retained memories were anonymous flat rows in
# ``astrocyte_vectors`` with no record of where they came from.
# M10 introduces the optional :class:`~astrocyte.provider.SourceStore`
# SPI which lets callers preserve the source ↔ chunk ↔ memory chain so
# that:
#
# 1. **Provenance** — every memory traces back through its chunk to its
#    originating document
# 2. **Dedup** — content_hash on documents and chunks prevents storing
#    duplicate sources twice
# 3. **Re-extraction** — chunks can be re-processed without losing the
#    original document text
# 4. **Source attribution** — recall results can answer "this answer
#    came from these chunks of these documents"
#
# Backward compat: existing memories without a chunk parent (chunk_id IS
# NULL on astrocyte_vectors) continue to work unchanged. Adoption is
# entirely opt-in at the call site.


@dataclass(frozen=True)
class SourceDocument:
    """A top-level source document we ingested into a bank.

    The "source" of one or more :class:`SourceChunk` rows, which in turn
    are the source of one or more memory rows in the vector store.

    Attributes:
        id: Stable identifier (caller-supplied, must be unique per bank).
        bank_id: Tenant-scoped bank identifier.
        title: Optional human-readable title (e.g. file name, page title).
        source_uri: Optional pointer back to where this document originated
            (URL, file path, message-bus offset, etc.). Free-form.
        content_hash: Optional SHA-256 (hex) of the ORIGINAL document
            content — used by :meth:`SourceStore.find_document_by_hash`
            for dedup before re-ingest.
        content_type: Optional MIME type (``text/plain``,
            ``application/pdf``, etc.).
        metadata: Free-form JSON-serialisable metadata.
        created_at: When the document was first stored (assigned by the
            store; placeholder values from callers are overwritten).
    """

    id: str
    bank_id: str
    title: str | None = None
    source_uri: str | None = None
    content_hash: str | None = None
    content_type: str | None = None
    metadata: Metadata | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class SourceChunk:
    """A chunk of a :class:`SourceDocument`.

    Sized + ordered by the chunking strategy used at retain time
    (paragraph / sentence / fixed-token / etc.). One chunk typically
    produces one memory in the vector store, but the relationship is
    1:N — a chunk can produce zero memories (e.g. policy filtered) or
    multiple (e.g. structured-fact extraction yielded N facts).

    Attributes:
        id: Stable identifier (caller-supplied OR generated by chunker).
        bank_id: Tenant-scoped bank identifier.
        document_id: ``SourceDocument.id`` this chunk belongs to.
        chunk_index: Ordering within the document, starts at 0.
        text: The chunk's text content.
        content_hash: SHA-256 (hex) of ``text`` — used for dedup so the
            same exact chunk ingested twice doesn't double up.
        metadata: Free-form metadata (e.g. character offsets, page
            numbers, speaker tags from chat transcripts).
        created_at: When the chunk was first stored.
    """

    id: str
    bank_id: str
    document_id: str
    chunk_index: int
    text: str
    content_hash: str | None = None
    metadata: Metadata | None = None
    created_at: datetime | None = None


@dataclass
class CompileScope:
    """A resolved compile scope — either from a tag or a DBSCAN cluster label."""

    scope: str  # Scope string (tag name or cluster label)
    source: Literal["tag", "cluster", "explicit"]  # How it was discovered
    memory_ids: list[str]  # Memory IDs belonging to this scope


@dataclass
class CompileRequest:
    bank_id: str
    scope: str | None = None  # If None, triggers full scope discovery (§3.2)


@dataclass
class CompileResult:
    """Result of a brain.compile() call."""

    bank_id: str
    scopes_compiled: list[str]  # Scope strings that produced wiki pages
    pages_created: int
    pages_updated: int
    noise_memories: int  # Untagged memories DBSCAN could not cluster (held for next cycle)
    tokens_used: int
    elapsed_ms: int
    error: str | None = None


# ---------------------------------------------------------------------------
# PII detection (used by policy layer)
# ---------------------------------------------------------------------------


@dataclass
class PiiMatch:
    pii_type: str  # "email", "phone", "ssn", "credit_card", "ip_address", …
    start: int
    end: int
    matched_text: str
    replacement: str | None = None
