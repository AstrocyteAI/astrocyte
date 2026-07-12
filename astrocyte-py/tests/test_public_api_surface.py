"""Public API surface snapshot.

``astrocyte.__all__`` is the package's public contract ("import from here,
not from submodules" — see ``astrocyte/__init__.py``). This test pins that
surface exactly:

- Adding a public name requires updating EXPECTED_PUBLIC_API here — making the
  addition a deliberate, reviewable diff rather than a side effect.
- Removing or renaming a name fails loudly — a breaking change that must go
  through the deprecation policy (product-roadmap.md § Stability).
- Every exported name must actually resolve on the package (no dangling
  re-exports).

Regenerate the list after an intentional change:
    uv run python -c "import astrocyte; print('\\n'.join(sorted(set(astrocyte.__all__))))"
"""

from __future__ import annotations

import astrocyte

EXPECTED_PUBLIC_API = frozenset([
    "AccessDenied",
    "AccessGrant",
    "ActorIdentity",
    "Astrocyte",
    "AstrocyteContext",
    "AstrocyteError",
    "AuditEvent",
    "BankHealth",
    "BankResolver",
    "CapabilityNotSupported",
    "Completion",
    "ConfigError",
    "ContentPart",
    "CrossBorderViolation",
    "DataClassification",
    "Dispositions",
    "Document",
    "DocumentFilters",
    "DocumentHit",
    "DocumentStore",
    "EngineCapabilities",
    "EngineProvider",
    "Entity",
    "EntityLink",
    "EvalMetrics",
    "EvalResult",
    "ForgetRequest",
    "ForgetResult",
    "ForgetSelector",
    "GraphHit",
    "GraphStore",
    "HealthIssue",
    "HealthStatus",
    "HookEvent",
    "HttpClientContext",
    "HybridEngineProvider",
    "IdentityConfig",
    "IngestError",
    "LLMCapabilities",
    "LLMProvider",
    "LegalHold",
    "LegalHoldActive",
    "LifecycleAction",
    "LifecycleRunResult",
    "log_safe",
    "MemoryEntityAssociation",
    "MemoryHit",
    "MemoryUsage",
    "Message",
    "Metadata",
    "MetadataValue",
    "MipRoutingError",
    "MultiBankStrategy",
    "OutboundTransportProvider",
    "PLACE_BANK",
    "PLACE_QUERY",
    "PiiMatch",
    "PiiRejected",
    "PreparedRetainInput",
    "ProviderUnavailable",
    "QualityDataPoint",
    "QueryResult",
    "RateLimited",
    "RecallRequest",
    "resolve_provider",
    "RecallResult",
    "RecallTrace",
    "ReflectRequest",
    "ReflectResult",
    "RegressionAlert",
    "RetainRequest",
    "RetainResult",
    "RoutingDecision",
    "TokenUsage",
    "TransportCapabilities",
    "VectorFilters",
    "VectorHit",
    "VectorItem",
    "VectorStore",
    "accessible_read_banks",
    "auth_with_oauth_cache_namespace",
    "build_proxy_headers",
    "clear_oauth2_token_cache_for_tests",
    "effective_permissions",
    "exchange_oauth2_authorization_code",
    "extraction_profile_for_source",
    "fetch_oauth2_client_credentials_token",
    "fetch_oauth2_refresh_access_token",
    "fetch_proxy_recall_hits",
    "format_principal",
    "gather_proxy_hits_for_bank",
    "merge_external_into_recall_result",
    "merge_manual_and_proxy_hits",
    "merged_extraction_profiles",
    "parse_principal",
    "post_oauth2_token_endpoint",
    "prepare_retain_input",
    "resolve_actor",
    "validate_proxy_recall_dns",
    "validate_proxy_recall_url",
])


def test_public_surface_matches_snapshot() -> None:
    actual = set(astrocyte.__all__)
    added = actual - EXPECTED_PUBLIC_API
    removed = EXPECTED_PUBLIC_API - actual
    assert not added and not removed, (
        f"Public API surface changed. Added: {sorted(added)}; removed: {sorted(removed)}. "
        "If intentional, update EXPECTED_PUBLIC_API (and follow the deprecation "
        "policy for removals — product-roadmap.md § Stability)."
    )


def test_every_exported_name_resolves() -> None:
    missing = [n for n in astrocyte.__all__ if not hasattr(astrocyte, n)]
    assert not missing, f"__all__ lists names that do not resolve: {missing}"


def test_no_duplicate_exports() -> None:
    assert len(astrocyte.__all__) == len(set(astrocyte.__all__))
