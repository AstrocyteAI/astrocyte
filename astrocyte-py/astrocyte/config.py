"""Astrocyte configuration — YAML loading, profile resolution, env var substitution."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from astrocyte.errors import ConfigError
from astrocyte.types import AccessGrant

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# ---------------------------------------------------------------------------
# Profile directory (shipped inside the package)
# ---------------------------------------------------------------------------
_PROFILES_DIR = Path(__file__).parent / "profiles"


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RateLimitConfig:
    retain_per_minute: int | None = None
    recall_per_minute: int | None = None
    reflect_per_minute: int | None = None
    global_per_minute: int | None = None


@dataclass
class QuotaConfig:
    retain_per_day: int | None = None
    reflect_per_day: int | None = None


@dataclass
class HomeostasisConfig:
    recall_max_tokens: int | None = None
    reflect_max_tokens: int | None = None
    retain_max_content_bytes: int | None = None
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    quotas: QuotaConfig = field(default_factory=QuotaConfig)


@dataclass
class PiiConfig:
    mode: str = "regex"  # "regex" | "ner" | "llm" | "rules_then_llm" | "disabled"
    action: str = "redact"  # "redact" | "reject" | "warn"
    patterns: list[dict[str, str]] | None = None
    countries: list[str] | None = None  # ["SG", "IN", "UK", "US", "DE", "FR", "IT", "ES", "AU", "CA", "JP", "CN"]
    type_overrides: dict[str, dict[str, str]] | None = None  # {"credit_card": {"action": "reject"}}


@dataclass
class ValidationConfig:
    max_content_length: int = 50000
    reject_empty_content: bool = True
    reject_binary_content: bool = True
    allowed_content_types: list[str] | None = None


@dataclass
class MetadataSanitizationConfig:
    blocked_keys: list[str] = field(default_factory=lambda: ["api_key", "password", "token", "secret"])
    max_metadata_size_bytes: int = 4096


@dataclass
class BarrierConfig:
    pii: PiiConfig = field(default_factory=PiiConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    metadata: MetadataSanitizationConfig = field(default_factory=MetadataSanitizationConfig)


@dataclass
class DedupConfig:
    enabled: bool = True
    similarity_threshold: float = 0.95
    action: str = "skip"  # "skip" | "warn" | "update"


@dataclass
class NoisyBankConfig:
    enabled: bool = True
    retain_spike_multiplier: float = 5.0
    min_avg_content_length: int = 20
    max_dedup_rate: float = 0.8
    action: str = "warn"  # "warn" | "throttle" | "reject"


@dataclass
class RecallCacheConfig:
    enabled: bool = False
    similarity_threshold: float = 0.95
    max_entries: int = 256
    ttl_seconds: float = 300.0


@dataclass
class TieredRetrievalConfig:
    enabled: bool = False
    min_results: int = 3
    min_score: float = 0.3
    max_tier: int = 3  # 0-4
    #: Which recall path runs at tier 3+ (and tier 4 after reformulation). ``pipeline`` = built-in
    #: pipeline only (default). ``hybrid`` = :class:`~astrocyte.hybrid.HybridEngineProvider` merge
    #: (engine + pipeline); requires a hybrid engine provider and ``tiered_retrieval.enabled``.
    full_recall: Literal["pipeline", "hybrid"] = "pipeline"


@dataclass
class RecallAuthorityTierConfig:
    """One precedence band for :class:`RecallAuthorityConfig` (matches ``metadata[\"authority_tier\"]``)."""

    id: str = ""
    priority: int = 1
    label: str = ""


@dataclass
class RecallAuthorityConfig:
    """Structured recall authority — labels fused hits for synthesis (M7)."""

    enabled: bool = False
    rules_inline: str | None = None
    rules_path: str | None = None
    tiers: list[RecallAuthorityTierConfig] = field(default_factory=list)
    #: When True, :meth:`Astrocyte.reflect` / pipeline reflect inject ``authority_context`` into the synthesis prompt.
    apply_to_reflect: bool = True
    #: Default ``metadata[\"authority_tier\"]`` for vectors in a bank (profile ``authority_tier`` overrides).
    tier_by_bank: dict[str, str] = field(default_factory=dict)


@dataclass
class CuratedRetainConfig:
    enabled: bool = False
    model: str | None = None
    context_recall_limit: int = 5


@dataclass
class CuratedRecallConfig:
    enabled: bool = False
    freshness_weight: float = 0.3
    reliability_weight: float = 0.2
    salience_weight: float = 0.2
    original_score_weight: float = 0.3
    freshness_half_life_days: float = 30.0
    min_score: float | None = None


@dataclass
class SignalQualityConfig:
    dedup: DedupConfig = field(default_factory=DedupConfig)
    noisy_bank: NoisyBankConfig = field(default_factory=NoisyBankConfig)


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    half_open_max_calls: int = 2


@dataclass
class EscalationConfig:
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    degraded_mode: str = "empty_recall"  # "empty_recall" | "error" | "cache"


@dataclass
class ObservabilityConfig:
    otel_enabled: bool = False
    prometheus_enabled: bool = False
    log_level: str = "info"


@dataclass
class AccessControlConfig:
    enabled: bool = False
    default_policy: str = "owner_only"  # "owner_only" | "open" | "deny"


@dataclass
class IdentityConfig:
    """Identity-driven bank resolution and ACL helpers (M1–M2 / v0.5.0)."""

    auto_resolve_banks: bool = False
    user_bank_prefix: str = "user-"
    agent_bank_prefix: str = "agent-"
    service_bank_prefix: str = "service-"
    resolver: Literal["convention", "config", "custom"] | None = None
    obo_enabled: bool = False


# ---------------------------------------------------------------------------
# M2 — Config schema evolution (ADR-003, v0.5.0 with M1)
# ---------------------------------------------------------------------------


@dataclass
class SourceConfig:
    """External data source definition (webhook ingest: see ``astrocyte.ingest``)."""

    type: str = ""
    extraction_profile: str | None = None
    target_bank: str | None = None
    target_bank_template: str | None = None
    principal: str | None = None
    auth: dict[str, str | int | float | bool | None] | None = None
    path: str | None = None
    driver: str | None = None
    topic: str | None = None
    consumer_group: str | None = None
    url: str | None = None
    interval_seconds: int | None = None
    # M4.1 proxy recall: GET (default) or POST JSON to ``url``
    recall_method: str | None = None  # "GET" | "POST"
    recall_body: Any | None = None  # POST JSON: dict/str with placeholders (see ``astrocyte.recall.proxy``)


@dataclass
class AgentRegistrationConfig:
    """Registered agent with bank access and optional rate hints (ADR-003 / v0.5.0)."""

    principal: str | None = None
    banks: list[str] | None = None
    allowed_banks: list[str] | None = None  # roadmap alias for banks; glob patterns allowed
    default_bank: str | None = None
    permissions: list[str] | None = None
    max_retain_per_minute: int | None = None
    max_recall_per_minute: int | None = None


@dataclass
class TlsConfig:
    cert_path: str | None = None
    key_path: str | None = None


@dataclass
class DeploymentConfig:
    """Standalone gateway settings; ignored in library mode."""

    mode: Literal["library", "standalone", "plugin"] = "library"
    host: str | None = None
    port: int | None = None
    workers: int | None = None
    cors_origins: list[str] | None = None
    tls: TlsConfig | None = None


@dataclass
class ExtractionProfileConfig:
    """Reusable extraction defaults for sources (pipeline implementation in M3)."""

    content_type: str | None = None
    chunking_strategy: str | None = None
    entity_extraction: bool | str | None = None
    metadata_mapping: dict[str, str] | None = None
    tag_rules: list[dict[str, str | list[str]]] | None = None
    chunk_size: int | None = None
    fact_type: str | None = None  # default "world"; e.g. "experience", "observation"
    #: Optional recall-authority band id (overrides ``recall_authority.tier_by_bank`` for this profile).
    authority_tier: str | None = None


@dataclass
class McpConfig:
    default_bank_id: str | None = None
    expose_reflect: bool = True
    expose_forget: bool = False
    max_results_limit: int = 50
    principal: str | None = None


@dataclass
class DefaultsConfig:
    """Per-profile default settings."""

    skepticism: int = 3
    literalism: int = 3
    empathy: int = 3
    preferred_fact_types: list[str] | None = None
    tags: list[str] | None = None


@dataclass
class DlpConfig:
    """Data Loss Prevention — output scanning for PII in recall/reflect results."""

    scan_recall_output: bool = False
    scan_reflect_output: bool = False
    output_pii_action: str = "warn"  # "redact" | "reject" | "warn"


@dataclass
class LifecycleTtlConfig:
    archive_after_days: int = 90  # Days since last recall before archiving
    delete_after_days: int = 365  # Days since creation before deletion
    exempt_tags: list[str] | None = None  # Tags that exempt from TTL
    fact_type_overrides: dict[str, int | None] | None = None  # Override archive_after_days by fact_type


@dataclass
class LifecycleConfig:
    enabled: bool = False
    ttl: LifecycleTtlConfig = field(default_factory=LifecycleTtlConfig)


@dataclass
class BankConfig:
    """Per-bank override settings."""

    profile: str | None = None
    access: list[dict[str, str | list[str]]] | None = None
    homeostasis: HomeostasisConfig | None = None
    barriers: BarrierConfig | None = None
    signal_quality: SignalQualityConfig | None = None


@dataclass
class AstrocyteConfig:
    """Top-level Astrocyte configuration."""

    # Provider tier
    provider_tier: Literal["storage", "engine"] = "engine"

    # Profile
    profile: str | None = None

    # Tier 2: Engine
    provider: str | None = None
    provider_config: dict[str, str | int | float | bool | None] | None = None

    # Tier 1: Storage
    vector_store: str | None = None
    vector_store_config: dict[str, str | int | float | bool | None] | None = None
    graph_store: str | None = None
    graph_store_config: dict[str, str | int | float | bool | None] | None = None
    document_store: str | None = None
    document_store_config: dict[str, str | int | float | bool | None] | None = None

    # LLM
    llm_provider: str | None = None
    llm_provider_config: dict[str, str | int | float | bool | None] | None = None
    embedding_provider: str | None = None
    embedding_provider_config: dict[str, str | int | float | bool | None] | None = None

    # Fallback
    fallback_strategy: str = "error"  # "local_llm" | "error" | "degrade"

    # Policy
    homeostasis: HomeostasisConfig = field(default_factory=HomeostasisConfig)
    barriers: BarrierConfig = field(default_factory=BarrierConfig)
    signal_quality: SignalQualityConfig = field(default_factory=SignalQualityConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    access_control: AccessControlConfig = field(default_factory=AccessControlConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)

    # MCP
    mcp: McpConfig = field(default_factory=McpConfig)

    # Phase 2 innovations
    recall_cache: RecallCacheConfig = field(default_factory=RecallCacheConfig)
    tiered_retrieval: TieredRetrievalConfig = field(default_factory=TieredRetrievalConfig)
    recall_authority: RecallAuthorityConfig = field(default_factory=RecallAuthorityConfig)
    curated_retain: CuratedRetainConfig = field(default_factory=CuratedRetainConfig)
    curated_recall: CuratedRecallConfig = field(default_factory=CuratedRecallConfig)

    # Compliance profile
    compliance_profile: str | None = None  # "gdpr" | "hipaa" | "pdpa" | None

    # DLP
    dlp: DlpConfig = field(default_factory=DlpConfig)

    # Lifecycle
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)

    # MIP (Memory Intent Protocol)
    mip_config_path: str | None = None  # Path to mip.yaml

    # Per-bank overrides
    banks: dict[str, BankConfig] | None = None

    # Top-level access grants (merged with banks.*.access by access_grants_for_astrocyte)
    access_grants: list[AccessGrant] | None = None

    # ADR-003 (v0.5.0 with M1)
    sources: dict[str, SourceConfig] | None = None
    agents: dict[str, AgentRegistrationConfig] | None = None
    deployment: DeploymentConfig | None = None
    extraction_profiles: dict[str, ExtractionProfileConfig] | None = None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _substitute_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} with environment variable values."""

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            return match.group(0)  # Leave unresolved
        return env_value

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _substitute_env_recursive(
    data: dict | list | str | int | float | bool | None,
) -> dict | list | str | int | float | bool | None:
    """Recursively substitute env vars in a parsed YAML structure."""
    if isinstance(data, str):
        return _substitute_env_vars(data)
    if isinstance(data, dict):
        return {k: _substitute_env_recursive(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_substitute_env_recursive(item) for item in data]
    return data


def _load_profile(profile_name: str) -> dict:
    """Load a profile YAML from the profiles directory or a file path."""
    if profile_name.startswith("./") or profile_name.startswith("/"):
        profile_path = Path(profile_name)
    else:
        profile_path = _PROFILES_DIR / f"{profile_name}.yaml"

    if not profile_path.exists():
        raise ConfigError(f"Profile not found: {profile_path}")

    with open(profile_path) as f:
        return yaml.safe_load(f) or {}


_COMPLIANCE_PROFILES_DIR = _PROFILES_DIR / "compliance"


def _load_compliance_profile(name: str) -> dict:
    """Load a compliance profile YAML (gdpr, hipaa, pdpa)."""
    if name.startswith("./") or name.startswith("/"):
        profile_path = Path(name)
    else:
        profile_path = _COMPLIANCE_PROFILES_DIR / f"{name}.yaml"

    if not profile_path.exists():
        raise ConfigError(f"Compliance profile not found: {profile_path}")

    with open(profile_path) as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base. Override values win."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _filter_dataclass_fields(cls: type, data: dict) -> dict:
    """Filter dict to only keys that are valid fields of the dataclass. Prevents TypeError on unknown keys."""
    import dataclasses

    valid = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in valid}


def _dict_to_config(data: dict) -> AstrocyteConfig:
    """Convert a flat/nested dict to AstrocyteConfig with nested dataclasses."""
    config = AstrocyteConfig()

    # Simple scalar fields
    for field_name in [
        "provider_tier",
        "profile",
        "provider",
        "provider_config",
        "vector_store",
        "vector_store_config",
        "graph_store",
        "graph_store_config",
        "document_store",
        "document_store_config",
        "llm_provider",
        "llm_provider_config",
        "embedding_provider",
        "embedding_provider_config",
        "fallback_strategy",
    ]:
        if field_name in data:
            setattr(config, field_name, data[field_name])

    # Nested config objects
    if "homeostasis" in data:
        h = data["homeostasis"]
        rl = h.get("rate_limits", {})
        q = h.get("quotas", {})
        config.homeostasis = HomeostasisConfig(
            recall_max_tokens=h.get("recall_max_tokens"),
            reflect_max_tokens=h.get("reflect_max_tokens"),
            retain_max_content_bytes=h.get("retain_max_content_bytes"),
            rate_limits=RateLimitConfig(
                **_filter_dataclass_fields(RateLimitConfig, {k: v for k, v in rl.items() if v is not None})
            ),
            quotas=QuotaConfig(**_filter_dataclass_fields(QuotaConfig, {k: v for k, v in q.items() if v is not None})),
        )

    if "barriers" in data:
        b = data["barriers"]
        pii_data = b.get("pii", {})
        val_data = b.get("validation", {})
        meta_data = b.get("metadata", {})
        config.barriers = BarrierConfig(
            pii=PiiConfig(**_filter_dataclass_fields(PiiConfig, pii_data)),
            validation=ValidationConfig(**_filter_dataclass_fields(ValidationConfig, val_data)),
            metadata=MetadataSanitizationConfig(**_filter_dataclass_fields(MetadataSanitizationConfig, meta_data)),
        )

    if "escalation" in data:
        e = data["escalation"]
        cb = e.get("circuit_breaker", {})
        config.escalation = EscalationConfig(
            circuit_breaker=CircuitBreakerConfig(**_filter_dataclass_fields(CircuitBreakerConfig, cb)),
            degraded_mode=e.get("degraded_mode", "empty_recall"),
        )

    if "observability" in data:
        config.observability = ObservabilityConfig(
            **_filter_dataclass_fields(ObservabilityConfig, data["observability"])
        )

    if "access_control" in data:
        config.access_control = AccessControlConfig(
            **_filter_dataclass_fields(AccessControlConfig, data["access_control"])
        )

    if "identity" in data:
        config.identity = IdentityConfig(**_filter_dataclass_fields(IdentityConfig, data["identity"]))

    if "defaults" in data:
        config.defaults = DefaultsConfig(**_filter_dataclass_fields(DefaultsConfig, data["defaults"]))

    if "mcp" in data:
        config.mcp = McpConfig(**_filter_dataclass_fields(McpConfig, data["mcp"]))

    if "signal_quality" in data:
        sq = data["signal_quality"]
        dedup_data = sq.get("dedup", {})
        noisy_data = sq.get("noisy_bank", {})
        config.signal_quality = SignalQualityConfig(
            dedup=DedupConfig(**_filter_dataclass_fields(DedupConfig, dedup_data)),
            noisy_bank=NoisyBankConfig(**_filter_dataclass_fields(NoisyBankConfig, noisy_data)),
        )

    if "recall_cache" in data:
        config.recall_cache = RecallCacheConfig(**_filter_dataclass_fields(RecallCacheConfig, data["recall_cache"]))

    if "tiered_retrieval" in data:
        config.tiered_retrieval = TieredRetrievalConfig(
            **_filter_dataclass_fields(TieredRetrievalConfig, data["tiered_retrieval"])
        )

    if "recall_authority" in data and isinstance(data["recall_authority"], dict):
        ra = data["recall_authority"]
        tiers_raw = ra.get("tiers") or []
        tiers: list[RecallAuthorityTierConfig] = []
        if isinstance(tiers_raw, list):
            for row in tiers_raw:
                if isinstance(row, dict):
                    tiers.append(RecallAuthorityTierConfig(**_filter_dataclass_fields(RecallAuthorityTierConfig, row)))
        tb = ra.get("tier_by_bank")
        tier_by_bank: dict[str, str] = {}
        if isinstance(tb, dict):
            tier_by_bank = {str(k): str(v) for k, v in tb.items()}
        config.recall_authority = RecallAuthorityConfig(
            enabled=bool(ra.get("enabled", False)),
            rules_inline=ra.get("rules_inline"),
            rules_path=ra.get("rules_path"),
            apply_to_reflect=bool(ra.get("apply_to_reflect", True)),
            tier_by_bank=tier_by_bank,
            tiers=tiers,
        )

    if "curated_retain" in data:
        config.curated_retain = CuratedRetainConfig(
            **_filter_dataclass_fields(CuratedRetainConfig, data["curated_retain"])
        )

    if "curated_recall" in data:
        config.curated_recall = CuratedRecallConfig(
            **_filter_dataclass_fields(CuratedRecallConfig, data["curated_recall"])
        )

    if "access_grants" in data and data["access_grants"]:
        grants: list[AccessGrant] = []
        for row in data["access_grants"]:
            if not isinstance(row, dict):
                continue
            grants.append(
                AccessGrant(
                    bank_id=str(row["bank_id"]),
                    principal=str(row["principal"]),
                    permissions=[str(p) for p in row["permissions"]],
                )
            )
        config.access_grants = grants

    if "compliance_profile" in data:
        config.compliance_profile = data["compliance_profile"]

    if "dlp" in data:
        config.dlp = DlpConfig(**_filter_dataclass_fields(DlpConfig, data["dlp"]))

    if "lifecycle" in data:
        lc = data["lifecycle"]
        ttl_data = lc.get("ttl", {})
        config.lifecycle = LifecycleConfig(
            enabled=lc.get("enabled", False),
            ttl=LifecycleTtlConfig(**_filter_dataclass_fields(LifecycleTtlConfig, ttl_data)),
        )

    if "mip_config_path" in data:
        config.mip_config_path = data["mip_config_path"]
    elif "mip" in data and isinstance(data["mip"], str):
        config.mip_config_path = data["mip"]

    if "banks" in data and data["banks"]:
        banks: dict[str, BankConfig] = {}
        for bid, bdata in data["banks"].items():
            if not isinstance(bdata, dict):
                continue
            bc = BankConfig(
                profile=bdata.get("profile"),
                access=bdata.get("access"),
            )
            if "homeostasis" in bdata and isinstance(bdata["homeostasis"], dict):
                h = bdata["homeostasis"]
                rl = h.get("rate_limits", {}) if isinstance(h.get("rate_limits"), dict) else {}
                q = h.get("quotas", {}) if isinstance(h.get("quotas"), dict) else {}
                bc.homeostasis = HomeostasisConfig(
                    recall_max_tokens=h.get("recall_max_tokens"),
                    reflect_max_tokens=h.get("reflect_max_tokens"),
                    retain_max_content_bytes=h.get("retain_max_content_bytes"),
                    rate_limits=RateLimitConfig(**_filter_dataclass_fields(RateLimitConfig, rl)),
                    quotas=QuotaConfig(**_filter_dataclass_fields(QuotaConfig, q)),
                )
            if "barriers" in bdata and isinstance(bdata["barriers"], dict):
                b = bdata["barriers"]
                pii_data = b.get("pii", {}) if isinstance(b.get("pii"), dict) else {}
                val_data = b.get("validation", {}) if isinstance(b.get("validation"), dict) else {}
                meta_data = b.get("metadata", {}) if isinstance(b.get("metadata"), dict) else {}
                bc.barriers = BarrierConfig(
                    pii=PiiConfig(**_filter_dataclass_fields(PiiConfig, pii_data)),
                    validation=ValidationConfig(**_filter_dataclass_fields(ValidationConfig, val_data)),
                    metadata=MetadataSanitizationConfig(
                        **_filter_dataclass_fields(MetadataSanitizationConfig, meta_data)
                    ),
                )
            if "signal_quality" in bdata and isinstance(bdata["signal_quality"], dict):
                sq = bdata["signal_quality"]
                dedup_data = sq.get("dedup", {}) if isinstance(sq.get("dedup"), dict) else {}
                noisy_data = sq.get("noisy_bank", {}) if isinstance(sq.get("noisy_bank"), dict) else {}
                bc.signal_quality = SignalQualityConfig(
                    dedup=DedupConfig(**_filter_dataclass_fields(DedupConfig, dedup_data)),
                    noisy_bank=NoisyBankConfig(**_filter_dataclass_fields(NoisyBankConfig, noisy_data)),
                )
            banks[str(bid)] = bc
        config.banks = banks

    if "extraction_profiles" in data and isinstance(data["extraction_profiles"], dict):
        profiles: dict[str, ExtractionProfileConfig] = {}
        for pname, pdata in data["extraction_profiles"].items():
            if isinstance(pdata, dict):
                profiles[str(pname)] = ExtractionProfileConfig(
                    **_filter_dataclass_fields(ExtractionProfileConfig, pdata)
                )
        config.extraction_profiles = profiles

    if "sources" in data and isinstance(data["sources"], dict):
        sources: dict[str, SourceConfig] = {}
        for sid, sdata in data["sources"].items():
            if isinstance(sdata, dict):
                sources[str(sid)] = SourceConfig(**_filter_dataclass_fields(SourceConfig, sdata))
        config.sources = sources

    if "agents" in data and isinstance(data["agents"], dict):
        agents: dict[str, AgentRegistrationConfig] = {}
        for aid, adata in data["agents"].items():
            if not isinstance(adata, dict):
                continue
            row = dict(adata)
            if row.get("banks") is None and row.get("allowed_banks") is not None:
                row["banks"] = list(row["allowed_banks"])
            agents[str(aid)] = AgentRegistrationConfig(**_filter_dataclass_fields(AgentRegistrationConfig, row))
        config.agents = agents

    if "deployment" in data and isinstance(data["deployment"], dict):
        dep = data["deployment"]
        tls: TlsConfig | None = None
        if isinstance(dep.get("tls"), dict):
            tls = TlsConfig(**_filter_dataclass_fields(TlsConfig, dep["tls"]))
        dep_no_tls = {k: v for k, v in dep.items() if k != "tls"}
        config.deployment = DeploymentConfig(
            **_filter_dataclass_fields(DeploymentConfig, dep_no_tls),
            tls=tls,
        )

    return config


def _agent_bank_list(ar: AgentRegistrationConfig) -> list[str]:
    if ar.banks:
        return list(ar.banks)
    if ar.allowed_banks:
        return list(ar.allowed_banks)
    return []


def _resolve_agent_bank_ids(
    patterns: list[str],
    declared: set[str] | None,
    *,
    label: str,
) -> list[str]:
    """Expand glob patterns against declared bank ids; validate literals when banks: is present."""
    if not patterns:
        return []
    out: list[str] = []
    for p in patterns:
        has_glob = any(c in p for c in "*?[")
        if has_glob:
            if not declared:
                raise ConfigError(f"{label}: bank pattern {p!r} uses wildcards but no banks: section is declared.")
            matches = sorted(bid for bid in declared if fnmatch.fnmatch(bid, p))
            if not matches:
                raise ConfigError(f"{label}: bank pattern {p!r} matches no declared banks.")
            out.extend(matches)
        elif declared is not None and p not in declared:
            raise ConfigError(f"{label}: bank id {p!r} is not listed under banks:.")
        else:
            out.append(p)
    return out


def validate_astrocyte_config(config: AstrocyteConfig) -> None:
    """Cross-field checks for ADR-003 sections (v0.5.0 with M1)."""
    if config.sources:
        from astrocyte.pipeline.extraction import merged_extraction_profiles

        profiles = merged_extraction_profiles(config)
        for name, src in config.sources.items():
            if not (src.type or "").strip():
                raise ConfigError(f"sources.{name}: type is required")
            st = (src.type or "").strip().lower()
            if st == "proxy":
                if not (src.url or "").strip():
                    raise ConfigError(f"sources.{name}: type proxy requires url")
                if not (src.target_bank or "").strip():
                    raise ConfigError(f"sources.{name}: type proxy requires target_bank")
            if src.extraction_profile:
                if src.extraction_profile not in profiles:
                    raise ConfigError(
                        f"sources.{name}: extraction_profile {src.extraction_profile!r} not found under extraction_profiles"
                    )

    if config.agents:
        declared = set(config.banks.keys()) if config.banks else None
        for agent_id, ar in config.agents.items():
            label = f"agents.{agent_id}"
            _resolve_agent_bank_ids(_agent_bank_list(ar), declared, label=label)

    ident = config.identity
    if ident.resolver is not None and ident.resolver not in ("convention", "config", "custom"):
        raise ConfigError(f"identity.resolver must be 'convention', 'config', or 'custom', got {ident.resolver!r}")

    if config.recall_authority.enabled and config.recall_authority.tiers:
        ra = config.recall_authority
        ids: list[str] = []
        for t in ra.tiers:
            tid = (t.id or "").strip()
            if not tid:
                raise ConfigError("recall_authority.tiers: each tier must have a non-empty id")
            ids.append(tid)
        if len(ids) != len(set(ids)):
            raise ConfigError("recall_authority.tiers: duplicate id")


def _grants_from_agents(config: AstrocyteConfig) -> list[AccessGrant]:
    if not config.agents:
        return []
    declared = set(config.banks.keys()) if config.banks else None
    out: list[AccessGrant] = []
    for agent_id, ar in config.agents.items():
        principal = ar.principal or f"agent:{agent_id}"
        perms = ar.permissions or ["read", "write"]
        label = f"agents.{agent_id}"
        for bid in _resolve_agent_bank_ids(_agent_bank_list(ar), declared, label=label):
            out.append(AccessGrant(bank_id=bid, principal=principal, permissions=list(perms)))
    return out


def _dedupe_grants(grants: list[AccessGrant]) -> list[AccessGrant]:
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    out: list[AccessGrant] = []
    for g in grants:
        key = (g.bank_id, g.principal, tuple(sorted(g.permissions)))
        if key in seen:
            continue
        seen.add(key)
        out.append(g)
    return out


def access_grants_for_astrocyte(config: AstrocyteConfig) -> list[AccessGrant]:
    """Flatten ``access_grants``, ``banks.*.access``, and ``agents:``-derived grants into one list for ``Astrocyte.set_access_grants``."""
    out: list[AccessGrant] = []
    if config.access_grants:
        out.extend(config.access_grants)
    if config.banks:
        for bank_id, bc in config.banks.items():
            if not bc.access:
                continue
            for row in bc.access:
                if not isinstance(row, dict):
                    continue
                out.append(
                    AccessGrant(
                        bank_id=bank_id,
                        principal=str(row["principal"]),
                        permissions=[str(p) for p in row["permissions"]],
                    )
                )
    out.extend(_grants_from_agents(config))
    return _dedupe_grants(out)


def load_config(path: str | Path) -> AstrocyteConfig:
    """Load Astrocyte configuration from a YAML file.

    Resolution order: compliance profile → behavior profile → user config → per-bank overrides.
    Environment variables are substituted (${VAR_NAME}).
    """
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    # Substitute environment variables
    raw = _substitute_env_recursive(raw)

    # Merge order: compliance (lowest) → behavior profile → user config (highest).
    # _deep_merge(base, override) → override wins.
    # Build base from lowest priority, then let higher priority layers override.
    base: dict = {}

    compliance_name = raw.get("compliance_profile")
    if compliance_name:
        compliance_data = _load_compliance_profile(compliance_name)
        base = _deep_merge(base, compliance_data)

    profile_name = raw.get("profile")
    if profile_name:
        profile_data = _load_profile(profile_name)
        base = _deep_merge(base, profile_data)

    # User config wins over everything
    merged = _deep_merge(base, raw)

    cfg = _dict_to_config(merged)
    validate_astrocyte_config(cfg)
    return cfg
