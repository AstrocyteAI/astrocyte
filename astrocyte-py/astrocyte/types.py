"""Astrocyte core types — all DTOs for the framework.

Every type here is FFI-safe: no Any, no callables, no generators.
Fields use only: str, int, float, bool, None, list, dict, datetime, dataclass.
See docs/_design/implementation-language-strategy.md for constraints.
"""

from __future__ import annotations

import json as _json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Literal

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


@dataclass
class VectorFilters:
    bank_id: str | None = None
    tags: list[str] | None = None
    fact_types: list[str] | None = None
    time_range: tuple[datetime, datetime] | None = None
    metadata_filters: Metadata | None = None


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


@dataclass
class EntityLink:
    source_entity_id: str
    target_entity_id: str
    link_type: str  # "works_at", "located_in", "related_to", …
    metadata: Metadata | None = None


@dataclass
class MemoryEntityAssociation:
    memory_id: str
    entity_id: str


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
    content_type: str = "text"  # "text", "conversation", "document"


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


@dataclass
class RecallTrace:
    strategies_used: list[str] | None = None
    total_candidates: int | None = None
    fusion_method: str | None = None
    latency_ms: float | None = None
    tier_used: int | None = None  # Which retrieval tier resolved the query
    layer_distribution: dict[str, int] | None = None  # {"fact": 5, "observation": 3, "model": 1}
    cache_hit: bool | None = None  # Whether recall cache was used


@dataclass
class RecallResult:
    hits: list[MemoryHit]
    total_available: int
    truncated: bool
    trace: RecallTrace | None = None


@dataclass
class Dispositions:
    """Personality modifiers for synthesis."""

    skepticism: int = 3  # 1 (trusting) to 5 (skeptical)
    literalism: int = 3  # 1 (flexible) to 5 (rigid)
    empathy: int = 3  # 1 (detached) to 5 (empathetic)


@dataclass
class ReflectRequest:
    query: str
    bank_id: str
    max_tokens: int | None = None
    include_sources: bool = True
    dispositions: Dispositions | None = None


@dataclass
class ReflectResult:
    answer: str
    confidence: float | None = None
    sources: list[MemoryHit] | None = None
    observations: list[str] | None = None


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


@dataclass
class Message:
    role: str  # "system", "user", "assistant"
    content: str | list[ContentPart] = ""


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class Completion:
    text: str
    model: str
    usage: TokenUsage | None = None


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


@dataclass
class AccessGrant:
    bank_id: str  # or "*"
    principal: str  # or "*"
    permissions: list[str]  # ["read", "write", "forget", "admin"]


@dataclass
class AstrocyteContext:
    """Caller identity for access control."""

    principal: str  # e.g. "agent:support-bot-1", "user:calvin"


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
