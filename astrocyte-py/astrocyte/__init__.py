"""Astrocyte — open-source memory framework for AI agents.

Public API surface. Import from here, not from submodules.
"""

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import IdentityConfig
from astrocyte.errors import (
    AccessDenied,
    AstrocyteError,
    CapabilityNotSupported,
    ConfigError,
    CrossBorderViolation,
    LegalHoldActive,
    MipRoutingError,
    PiiRejected,
    ProviderUnavailable,
    RateLimited,
)
from astrocyte.hybrid import HybridEngineProvider
from astrocyte.identity import (
    BankResolver,
    accessible_read_banks,
    effective_permissions,
    format_principal,
    parse_principal,
    resolve_actor,
)
from astrocyte.pipeline import (
    PreparedRetainInput,
    extraction_profile_for_source,
    merged_extraction_profiles,
    prepare_retain_input,
)
from astrocyte.provider import (
    DocumentStore,
    EngineProvider,
    GraphStore,
    LLMProvider,
    OutboundTransportProvider,
    VectorStore,
)
from astrocyte.types import (
    AccessGrant,
    ActorIdentity,
    AstrocyteContext,
    AuditEvent,
    BankHealth,
    Completion,
    ContentPart,
    DataClassification,
    Dispositions,
    Document,
    DocumentFilters,
    DocumentHit,
    EngineCapabilities,
    Entity,
    EntityLink,
    EvalMetrics,
    EvalResult,
    ForgetRequest,
    ForgetResult,
    ForgetSelector,
    GraphHit,
    HealthIssue,
    HealthStatus,
    HookEvent,
    HttpClientContext,
    LegalHold,
    LifecycleAction,
    LifecycleRunResult,
    LLMCapabilities,
    MemoryEntityAssociation,
    MemoryHit,
    MemoryUsage,
    Message,
    Metadata,
    MetadataValue,
    MultiBankStrategy,
    PiiMatch,
    QualityDataPoint,
    QueryResult,
    RecallRequest,
    RecallResult,
    RecallTrace,
    ReflectRequest,
    ReflectResult,
    RegressionAlert,
    RetainRequest,
    RetainResult,
    RoutingDecision,
    TokenUsage,
    TransportCapabilities,
    VectorFilters,
    VectorHit,
    VectorItem,
)

__all__ = [
    # Main class
    "Astrocyte",
    "HybridEngineProvider",
    # M3 extraction (stable imports)
    "prepare_retain_input",
    "merged_extraction_profiles",
    "extraction_profile_for_source",
    "PreparedRetainInput",
    # Errors
    "AstrocyteError",
    "ConfigError",
    "CapabilityNotSupported",
    "AccessDenied",
    "RateLimited",
    "ProviderUnavailable",
    "PiiRejected",
    "CrossBorderViolation",
    "LegalHoldActive",
    "MipRoutingError",
    # Protocols
    "VectorStore",
    "GraphStore",
    "DocumentStore",
    "EngineProvider",
    "LLMProvider",
    "OutboundTransportProvider",
    # Types — common
    "HealthStatus",
    "Metadata",
    "MetadataValue",
    # Types — vector store
    "VectorItem",
    "VectorFilters",
    "VectorHit",
    # Types — graph store
    "Entity",
    "EntityLink",
    "MemoryEntityAssociation",
    "GraphHit",
    # Types — document store
    "Document",
    "DocumentFilters",
    "DocumentHit",
    # Types — engine
    "RetainRequest",
    "RetainResult",
    "RecallRequest",
    "RecallResult",
    "MemoryHit",
    "RecallTrace",
    "ReflectRequest",
    "ReflectResult",
    "Dispositions",
    "ForgetRequest",
    "ForgetResult",
    "EngineCapabilities",
    # Types — LLM
    "Message",
    "ContentPart",
    "Completion",
    "TokenUsage",
    "LLMCapabilities",
    # Types — transport
    "HttpClientContext",
    "TransportCapabilities",
    # Types — multi-bank
    "MultiBankStrategy",
    # Types — access control
    "AccessGrant",
    "ActorIdentity",
    "AstrocyteContext",
    "IdentityConfig",
    # Identity (M1)
    "BankResolver",
    "resolve_actor",
    "format_principal",
    "parse_principal",
    "effective_permissions",
    "accessible_read_banks",
    # Types — hooks
    "HookEvent",
    # Types — governance
    "DataClassification",
    # Types — lifecycle
    "AuditEvent",
    "ForgetSelector",
    "LegalHold",
    "LifecycleAction",
    "LifecycleRunResult",
    # Types — MIP
    "RoutingDecision",
    # Types — analytics
    "BankHealth",
    "HealthIssue",
    "MemoryUsage",
    "QualityDataPoint",
    # Types — evaluation
    "EvalMetrics",
    "EvalResult",
    "QueryResult",
    "RegressionAlert",
    # Types — PII
    "PiiMatch",
]
