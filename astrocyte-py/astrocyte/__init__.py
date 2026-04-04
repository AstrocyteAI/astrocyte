"""Astrocyte — open-source memory framework for AI agents.

Public API surface. Import from here, not from submodules.
"""

from astrocyte._astrocyte import Astrocyte
from astrocyte.errors import (
    AccessDenied,
    AstrocyteError,
    CapabilityNotSupported,
    ConfigError,
    CrossBorderViolation,
    LegalHoldActive,
    PiiRejected,
    ProviderUnavailable,
    RateLimited,
)
from astrocyte.hybrid import HybridEngineProvider
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
    "AstrocyteContext",
    # Types — hooks
    "HookEvent",
    # Types — governance
    "DataClassification",
    # Types — lifecycle
    "AuditEvent",
    "ForgetSelector",
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
