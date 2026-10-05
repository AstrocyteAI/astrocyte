"""Astrocyte — open-source memory framework for AI agents.

Public API surface. Import from here, not from submodules.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

# Every public name is imported on first use (PEP 562), not when the package
# is: importing it eagerly pulls in httpx, the providers and every type, ~0.15
# s that each agent hook process (a fresh interpreter per prompt) paid before
# doing anything. Type checkers and API tools read the imports below.
if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte

    # Promoted internals: symbols external packages (e.g. the HTTP gateway)
    # legitimately need. Exported here so consumers never import astrocyte._*
    # modules directly — enforced by import-linter contracts.
    from astrocyte._discovery import resolve_provider
    from astrocyte._log_safety import safe as log_safe
    from astrocyte.config import IdentityConfig
    from astrocyte.errors import (
        AccessDenied,
        AstrocyteError,
        CapabilityNotSupported,
        ConfigError,
        CrossBorderViolation,
        IngestError,
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
    from astrocyte.recall import (
        PLACE_BANK,
        PLACE_QUERY,
        auth_with_oauth_cache_namespace,
        build_proxy_headers,
        clear_oauth2_token_cache_for_tests,
        exchange_oauth2_authorization_code,
        fetch_oauth2_client_credentials_token,
        fetch_oauth2_refresh_access_token,
        fetch_proxy_recall_hits,
        gather_proxy_hits_for_bank,
        merge_external_into_recall_result,
        merge_manual_and_proxy_hits,
        post_oauth2_token_endpoint,
        validate_proxy_recall_dns,
        validate_proxy_recall_url,
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
        MemoryChange,
        MemoryChangePage,
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
        SyncPushRecord,
        SyncPushResult,
        TokenUsage,
        TransportCapabilities,
        VectorFilters,
        VectorHit,
        VectorItem,
    )

_EXPORTS: dict[str, tuple[str, str]] = {
    "Astrocyte": ("astrocyte._astrocyte", "Astrocyte"),
    "resolve_provider": ("astrocyte._discovery", "resolve_provider"),
    "log_safe": ("astrocyte._log_safety", "safe"),
    "IdentityConfig": ("astrocyte.config", "IdentityConfig"),
    "AccessDenied": ("astrocyte.errors", "AccessDenied"),
    "AstrocyteError": ("astrocyte.errors", "AstrocyteError"),
    "CapabilityNotSupported": ("astrocyte.errors", "CapabilityNotSupported"),
    "ConfigError": ("astrocyte.errors", "ConfigError"),
    "CrossBorderViolation": ("astrocyte.errors", "CrossBorderViolation"),
    "IngestError": ("astrocyte.errors", "IngestError"),
    "LegalHoldActive": ("astrocyte.errors", "LegalHoldActive"),
    "MipRoutingError": ("astrocyte.errors", "MipRoutingError"),
    "PiiRejected": ("astrocyte.errors", "PiiRejected"),
    "ProviderUnavailable": ("astrocyte.errors", "ProviderUnavailable"),
    "RateLimited": ("astrocyte.errors", "RateLimited"),
    "HybridEngineProvider": ("astrocyte.hybrid", "HybridEngineProvider"),
    "BankResolver": ("astrocyte.identity", "BankResolver"),
    "accessible_read_banks": ("astrocyte.identity", "accessible_read_banks"),
    "effective_permissions": ("astrocyte.identity", "effective_permissions"),
    "format_principal": ("astrocyte.identity", "format_principal"),
    "parse_principal": ("astrocyte.identity", "parse_principal"),
    "resolve_actor": ("astrocyte.identity", "resolve_actor"),
    "PreparedRetainInput": ("astrocyte.pipeline", "PreparedRetainInput"),
    "extraction_profile_for_source": ("astrocyte.pipeline", "extraction_profile_for_source"),
    "merged_extraction_profiles": ("astrocyte.pipeline", "merged_extraction_profiles"),
    "prepare_retain_input": ("astrocyte.pipeline", "prepare_retain_input"),
    "DocumentStore": ("astrocyte.provider", "DocumentStore"),
    "EngineProvider": ("astrocyte.provider", "EngineProvider"),
    "GraphStore": ("astrocyte.provider", "GraphStore"),
    "LLMProvider": ("astrocyte.provider", "LLMProvider"),
    "OutboundTransportProvider": ("astrocyte.provider", "OutboundTransportProvider"),
    "VectorStore": ("astrocyte.provider", "VectorStore"),
    "PLACE_BANK": ("astrocyte.recall", "PLACE_BANK"),
    "PLACE_QUERY": ("astrocyte.recall", "PLACE_QUERY"),
    "auth_with_oauth_cache_namespace": ("astrocyte.recall", "auth_with_oauth_cache_namespace"),
    "build_proxy_headers": ("astrocyte.recall", "build_proxy_headers"),
    "clear_oauth2_token_cache_for_tests": ("astrocyte.recall", "clear_oauth2_token_cache_for_tests"),
    "exchange_oauth2_authorization_code": ("astrocyte.recall", "exchange_oauth2_authorization_code"),
    "fetch_oauth2_client_credentials_token": ("astrocyte.recall", "fetch_oauth2_client_credentials_token"),
    "fetch_oauth2_refresh_access_token": ("astrocyte.recall", "fetch_oauth2_refresh_access_token"),
    "fetch_proxy_recall_hits": ("astrocyte.recall", "fetch_proxy_recall_hits"),
    "gather_proxy_hits_for_bank": ("astrocyte.recall", "gather_proxy_hits_for_bank"),
    "merge_external_into_recall_result": ("astrocyte.recall", "merge_external_into_recall_result"),
    "merge_manual_and_proxy_hits": ("astrocyte.recall", "merge_manual_and_proxy_hits"),
    "post_oauth2_token_endpoint": ("astrocyte.recall", "post_oauth2_token_endpoint"),
    "validate_proxy_recall_dns": ("astrocyte.recall", "validate_proxy_recall_dns"),
    "validate_proxy_recall_url": ("astrocyte.recall", "validate_proxy_recall_url"),
    "AccessGrant": ("astrocyte.types", "AccessGrant"),
    "ActorIdentity": ("astrocyte.types", "ActorIdentity"),
    "AstrocyteContext": ("astrocyte.types", "AstrocyteContext"),
    "AuditEvent": ("astrocyte.types", "AuditEvent"),
    "BankHealth": ("astrocyte.types", "BankHealth"),
    "Completion": ("astrocyte.types", "Completion"),
    "ContentPart": ("astrocyte.types", "ContentPart"),
    "DataClassification": ("astrocyte.types", "DataClassification"),
    "Dispositions": ("astrocyte.types", "Dispositions"),
    "Document": ("astrocyte.types", "Document"),
    "DocumentFilters": ("astrocyte.types", "DocumentFilters"),
    "DocumentHit": ("astrocyte.types", "DocumentHit"),
    "EngineCapabilities": ("astrocyte.types", "EngineCapabilities"),
    "Entity": ("astrocyte.types", "Entity"),
    "EntityLink": ("astrocyte.types", "EntityLink"),
    "EvalMetrics": ("astrocyte.types", "EvalMetrics"),
    "EvalResult": ("astrocyte.types", "EvalResult"),
    "ForgetRequest": ("astrocyte.types", "ForgetRequest"),
    "ForgetResult": ("astrocyte.types", "ForgetResult"),
    "ForgetSelector": ("astrocyte.types", "ForgetSelector"),
    "GraphHit": ("astrocyte.types", "GraphHit"),
    "HealthIssue": ("astrocyte.types", "HealthIssue"),
    "HealthStatus": ("astrocyte.types", "HealthStatus"),
    "HookEvent": ("astrocyte.types", "HookEvent"),
    "HttpClientContext": ("astrocyte.types", "HttpClientContext"),
    "LegalHold": ("astrocyte.types", "LegalHold"),
    "LifecycleAction": ("astrocyte.types", "LifecycleAction"),
    "LifecycleRunResult": ("astrocyte.types", "LifecycleRunResult"),
    "LLMCapabilities": ("astrocyte.types", "LLMCapabilities"),
    "MemoryChange": ("astrocyte.types", "MemoryChange"),
    "MemoryChangePage": ("astrocyte.types", "MemoryChangePage"),
    "MemoryEntityAssociation": ("astrocyte.types", "MemoryEntityAssociation"),
    "MemoryHit": ("astrocyte.types", "MemoryHit"),
    "MemoryUsage": ("astrocyte.types", "MemoryUsage"),
    "Message": ("astrocyte.types", "Message"),
    "Metadata": ("astrocyte.types", "Metadata"),
    "MetadataValue": ("astrocyte.types", "MetadataValue"),
    "MultiBankStrategy": ("astrocyte.types", "MultiBankStrategy"),
    "PiiMatch": ("astrocyte.types", "PiiMatch"),
    "QualityDataPoint": ("astrocyte.types", "QualityDataPoint"),
    "QueryResult": ("astrocyte.types", "QueryResult"),
    "RecallRequest": ("astrocyte.types", "RecallRequest"),
    "RecallResult": ("astrocyte.types", "RecallResult"),
    "RecallTrace": ("astrocyte.types", "RecallTrace"),
    "ReflectRequest": ("astrocyte.types", "ReflectRequest"),
    "ReflectResult": ("astrocyte.types", "ReflectResult"),
    "RegressionAlert": ("astrocyte.types", "RegressionAlert"),
    "RetainRequest": ("astrocyte.types", "RetainRequest"),
    "RetainResult": ("astrocyte.types", "RetainResult"),
    "RoutingDecision": ("astrocyte.types", "RoutingDecision"),
    "SyncPushRecord": ("astrocyte.types", "SyncPushRecord"),
    "SyncPushResult": ("astrocyte.types", "SyncPushResult"),
    "TokenUsage": ("astrocyte.types", "TokenUsage"),
    "TransportCapabilities": ("astrocyte.types", "TransportCapabilities"),
    "VectorFilters": ("astrocyte.types", "VectorFilters"),
    "VectorHit": ("astrocyte.types", "VectorHit"),
    "VectorItem": ("astrocyte.types", "VectorItem"),
}


def __getattr__(name: str) -> Any:
    try:
        module, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


__all__ = [  # noqa: RUF022 — grouped by category, not alphabetically
    # Main class
    "Astrocyte",
    "HybridEngineProvider",
    # Promoted internals (SPI discovery, log-injection scrubbing)
    "resolve_provider",
    "log_safe",
    # M3 extraction (stable imports)
    "prepare_retain_input",
    "merged_extraction_profiles",
    "extraction_profile_for_source",
    "PreparedRetainInput",
    # M4.1 federated recall
    "PLACE_QUERY",
    "PLACE_BANK",
    "auth_with_oauth_cache_namespace",
    "build_proxy_headers",
    "clear_oauth2_token_cache_for_tests",
    "exchange_oauth2_authorization_code",
    "fetch_oauth2_client_credentials_token",
    "fetch_oauth2_refresh_access_token",
    "fetch_proxy_recall_hits",
    "gather_proxy_hits_for_bank",
    "merge_manual_and_proxy_hits",
    "merge_external_into_recall_result",
    "post_oauth2_token_endpoint",
    "validate_proxy_recall_dns",
    "validate_proxy_recall_url",
    # Errors
    "AstrocyteError",
    "ConfigError",
    "IngestError",
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
    "MemoryChange",
    "MemoryChangePage",
    "SyncPushRecord",
    "SyncPushResult",
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
