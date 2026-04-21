"""Astrocyte configuration — YAML loading, profile resolution, env var substitution."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

import yaml

from astrocyte.errors import ConfigError
from astrocyte.types import AccessGrant

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_BASE_SENSITIVE_FIELD_KEYS = (
    "api_key",
    "password",
    "token",
    "secret",
)

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
    countries: list[str] | None = None  # ["SG", "IN", "GB", "US", "DE", "FR", "IT", "ES", "AU", "CA", "JP", "CN"]
    type_overrides: dict[str, dict[str, str]] | None = None  # {"credit_card": {"action": "reject"}}


@dataclass
class ValidationConfig:
    max_content_length: int = 50000
    reject_empty_content: bool = True
    reject_binary_content: bool = True
    allowed_content_types: list[str] | None = None


@dataclass
class MetadataSanitizationConfig:
    blocked_keys: list[str] = field(default_factory=lambda: list(_BASE_SENSITIVE_FIELD_KEYS))
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
class JwtMiddlewareConfig:
    """JWT identity middleware (identity spec §3 Gap 1 wiring).

    When ``enabled``, the MCP server extracts the ``Authorization: Bearer``
    token from each inbound request, validates it against the configured
    JWKS, classifies the claims via :mod:`astrocyte.identity_jwt`, and
    populates :attr:`AstrocyteContext.actor` with a resolved
    :class:`ActorIdentity` for the call. See
    ``docs/_plugins/jwt-identity-middleware.md`` for the operator guide.

    When ``enabled=False`` (default), the MCP server preserves pre-middleware
    behavior: a single static ``AstrocyteContext`` is used for all calls.
    """

    enabled: bool = False
    #: JWKS endpoint for signature key retrieval. Required when enabled.
    jwks_uri: str | None = None
    #: Expected token ``aud`` claim. Required when enabled — unset audience
    #: is a common misconfiguration that can result in cross-tenant accepts.
    token_audience: str | None = None
    #: Expected token ``iss`` claim. Validated when set; left unchecked when
    #: None (some IdPs rotate issuers).
    token_issuer: str | None = None
    #: Signing algorithms accepted. Defaults to asymmetric only so HS* keys
    #: stolen from misconfigured deployments can't forge tokens.
    algorithms: list[str] = field(default_factory=lambda: ["RS256", "ES256"])
    #: When True, a missing or malformed Authorization header raises.
    #: When False (with ``allow_anonymous=True``), falls through to anonymous.
    fail_closed: bool = True
    #: Permit calls with no Authorization header. Ignored when fail_closed=True.
    allow_anonymous: bool = False
    #: JWKS cache refresh interval. Most JWKS endpoints rotate every 24h.
    jwks_refresh_interval_hours: int = 24


@dataclass
class IdentityConfig:
    """Identity-driven bank resolution and ACL helpers (M1–M2 / v0.5.0)."""

    auto_resolve_banks: bool = False
    user_bank_prefix: str = "user-"
    agent_bank_prefix: str = "agent-"
    service_bank_prefix: str = "service-"
    resolver: Literal["convention", "config", "custom"] | None = None
    obo_enabled: bool = False
    #: JWT identity middleware wiring (identity spec §3 Gap 1).
    jwt_middleware: JwtMiddlewareConfig = field(default_factory=JwtMiddlewareConfig)


# ---------------------------------------------------------------------------
# M2 — Config schema evolution (ADR-003, v0.5.0 with M1)
# ---------------------------------------------------------------------------


@dataclass
class SourceConfig:
    """External data source definition (``astrocyte.ingest``).

    * **webhook** — HTTP push; gateway / ASGI binds the route.
    * **stream** — long-running consumer — ``driver: redis`` (Redis Streams) or ``kafka`` (Kafka).
      Requires ``url``, ``topic``, ``consumer_group``, ``target_bank`` / ``target_bank_template``.
      For Redis, ``url`` is a Redis URL; for Kafka, ``url`` is bootstrap servers (e.g. ``localhost:9092``).
      Optional ``path``: Redis consumer name or Kafka ``client_id``.
    * **poll** / **api_poll** — scheduled HTTP pull — ``driver: github`` (``astrocyte-ingestion-github``).
      Requires ``interval_seconds``, ``path`` as ``owner/repo``, ``target_bank`` (or template), and
      ``auth.token`` (or env-substituted) for the GitHub API. Optional ``url`` overrides the API base
      (default ``https://api.github.com``; use GitHub Enterprise ``.../api/v3`` when needed).
    """

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


def _find_unresolved_env_vars(
    data: dict | list | str | int | float | bool | None,
    path: str = "",
) -> list[str]:
    """Find all unresolved ${VAR_NAME} references after env var substitution.

    Returns list of strings like "vector_store_config.dsn: ${DATABASE_URL}".
    """
    unresolved: list[str] = []
    if isinstance(data, str):
        for match in _ENV_VAR_PATTERN.finditer(data):
            unresolved.append(f"{path}: ${{{match.group(1)}}}")
    elif isinstance(data, dict):
        for k, v in data.items():
            child_path = f"{path}.{k}" if path else str(k)
            unresolved.extend(_find_unresolved_env_vars(v, child_path))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            unresolved.extend(_find_unresolved_env_vars(v, f"{path}[{i}]"))
    return unresolved


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

    try:
        with open(profile_path) as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {profile_path}: {exc}") from exc


_COMPLIANCE_PROFILES_DIR = _PROFILES_DIR / "compliance"


def _load_compliance_profile(name: str) -> dict:
    """Load a compliance profile YAML (gdpr, hipaa, pdpa)."""
    if name.startswith("./") or name.startswith("/"):
        profile_path = Path(name)
    else:
        profile_path = _COMPLIANCE_PROFILES_DIR / f"{name}.yaml"

    if not profile_path.exists():
        raise ConfigError(f"Compliance profile not found: {profile_path}")

    try:
        with open(profile_path) as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {profile_path}: {exc}") from exc


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base. Override values win."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _filter_dataclass_fields(cls: type, data: dict, *, drop_none: bool = False) -> dict:
    """Filter dict to valid dataclass fields; optionally drop ``None`` values."""
    valid = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in valid and (not drop_none or v is not None)}


_SENSITIVE_FIELD_PATTERNS = frozenset(
    _BASE_SENSITIVE_FIELD_KEYS
    + (
        "dsn",
        "connection_string",
        "credentials",
        "auth",
        "jwks_url",
        "issuer",
        "audience",
    )
)


def _is_sensitive_field(ref: str) -> bool:
    """Return True if an unresolved env-var reference is in a security-sensitive field."""
    # ref format: "path.to.field: ${VAR}" — extract field name before the colon
    field_path = ref.split(":")[0].strip().lower()
    return any(pat in field_path for pat in _SENSITIVE_FIELD_PATTERNS)


def _safe_sub_dict(data: dict, key: str) -> dict:
    """Safely extract a nested dict from *data*, defaulting to ``{}``."""
    val = data.get(key)
    return val if isinstance(val, dict) else {}


def _parse_homeostasis(data: dict) -> HomeostasisConfig:
    """Parse a homeostasis config block (used at top-level and per-bank)."""
    rl = _safe_sub_dict(data, "rate_limits")
    q = _safe_sub_dict(data, "quotas")
    return HomeostasisConfig(
        recall_max_tokens=data.get("recall_max_tokens"),
        reflect_max_tokens=data.get("reflect_max_tokens"),
        retain_max_content_bytes=data.get("retain_max_content_bytes"),
        rate_limits=RateLimitConfig(**_filter_dataclass_fields(RateLimitConfig, rl, drop_none=True)),
        quotas=QuotaConfig(**_filter_dataclass_fields(QuotaConfig, q, drop_none=True)),
    )


def _parse_barriers(data: dict) -> BarrierConfig:
    """Parse a barriers config block (used at top-level and per-bank)."""
    return BarrierConfig(
        pii=PiiConfig(**_filter_dataclass_fields(PiiConfig, _safe_sub_dict(data, "pii"))),
        validation=ValidationConfig(**_filter_dataclass_fields(ValidationConfig, _safe_sub_dict(data, "validation"))),
        metadata=MetadataSanitizationConfig(**_filter_dataclass_fields(MetadataSanitizationConfig, _safe_sub_dict(data, "metadata"))),
    )


def _parse_signal_quality(data: dict) -> SignalQualityConfig:
    """Parse a signal_quality config block (used at top-level and per-bank)."""
    return SignalQualityConfig(
        dedup=DedupConfig(**_filter_dataclass_fields(DedupConfig, _safe_sub_dict(data, "dedup"))),
        noisy_bank=NoisyBankConfig(**_filter_dataclass_fields(NoisyBankConfig, _safe_sub_dict(data, "noisy_bank"))),
    )


def _parse_escalation(data: dict) -> EscalationConfig:
    """Parse an ``escalation:`` config block."""
    cb = data.get("circuit_breaker", {})
    return EscalationConfig(
        circuit_breaker=CircuitBreakerConfig(**_filter_dataclass_fields(CircuitBreakerConfig, cb)),
        degraded_mode=data.get("degraded_mode", "empty_recall"),
    )


def _parse_recall_authority(data: dict) -> RecallAuthorityConfig:
    """Parse a ``recall_authority:`` config block."""
    tiers_raw = data.get("tiers") or []
    tiers: list[RecallAuthorityTierConfig] = []
    if isinstance(tiers_raw, list):
        for row in tiers_raw:
            if isinstance(row, dict):
                tiers.append(RecallAuthorityTierConfig(**_filter_dataclass_fields(RecallAuthorityTierConfig, row)))
    tb = data.get("tier_by_bank")
    tier_by_bank: dict[str, str] = {}
    if isinstance(tb, dict):
        tier_by_bank = {str(k): str(v) for k, v in tb.items()}
    return RecallAuthorityConfig(
        enabled=bool(data.get("enabled", False)),
        rules_inline=data.get("rules_inline"),
        rules_path=data.get("rules_path"),
        apply_to_reflect=bool(data.get("apply_to_reflect", True)),
        tier_by_bank=tier_by_bank,
        tiers=tiers,
    )


def _parse_access_grants(data: list) -> list[AccessGrant]:
    """Parse an ``access_grants:`` list, validating required fields."""
    grants: list[AccessGrant] = []
    for idx, row in enumerate(data):
        if not isinstance(row, dict):
            continue
        required_keys = ("bank_id", "principal", "permissions")
        missing = [k for k in required_keys if k not in row]
        if missing:
            raise ConfigError(
                f"Invalid access_grants entry at index {idx}: missing required field(s): {', '.join(missing)}"
            )
        if not isinstance(row["permissions"], list):
            raise ConfigError(
                f"Invalid access_grants entry at index {idx}: 'permissions' must be a list."
            )
        grants.append(
            AccessGrant(
                bank_id=str(row["bank_id"]),
                principal=str(row["principal"]),
                permissions=[str(p) for p in row["permissions"]],
            )
        )
    return grants


def _parse_lifecycle(data: dict) -> LifecycleConfig:
    """Parse a ``lifecycle:`` config block."""
    ttl_data = data.get("ttl", {})
    return LifecycleConfig(
        enabled=data.get("enabled", False),
        ttl=LifecycleTtlConfig(**_filter_dataclass_fields(LifecycleTtlConfig, ttl_data)),
    )


def _parse_banks(data: dict) -> dict[str, BankConfig]:
    """Parse a ``banks:`` config block with per-bank overrides."""
    banks: dict[str, BankConfig] = {}
    for bid, bdata in data.items():
        if not isinstance(bdata, dict):
            continue
        bc = BankConfig(
            profile=bdata.get("profile"),
            access=bdata.get("access"),
        )
        if "homeostasis" in bdata and isinstance(bdata["homeostasis"], dict):
            bc.homeostasis = _parse_homeostasis(bdata["homeostasis"])
        if "barriers" in bdata and isinstance(bdata["barriers"], dict):
            bc.barriers = _parse_barriers(bdata["barriers"])
        if "signal_quality" in bdata and isinstance(bdata["signal_quality"], dict):
            bc.signal_quality = _parse_signal_quality(bdata["signal_quality"])
        banks[str(bid)] = bc
    return banks


def _parse_agents(data: dict) -> dict[str, AgentRegistrationConfig]:
    """Parse an ``agents:`` config block."""
    agents: dict[str, AgentRegistrationConfig] = {}
    for aid, adata in data.items():
        if not isinstance(adata, dict):
            continue
        row = dict(adata)
        if row.get("banks") is None and row.get("allowed_banks") is not None:
            row["banks"] = list(row["allowed_banks"])
        agents[str(aid)] = AgentRegistrationConfig(**_filter_dataclass_fields(AgentRegistrationConfig, row))
    return agents


def _parse_deployment(data: dict) -> DeploymentConfig:
    """Parse a ``deployment:`` config block."""
    tls: TlsConfig | None = None
    if isinstance(data.get("tls"), dict):
        tls = TlsConfig(**_filter_dataclass_fields(TlsConfig, data["tls"]))
    dep_no_tls = {k: v for k, v in data.items() if k != "tls"}
    return DeploymentConfig(
        **_filter_dataclass_fields(DeploymentConfig, dep_no_tls),
        tls=tls,
    )


# Fields copied verbatim from the YAML dict onto AstrocyteConfig.
_SCALAR_CONFIG_FIELDS = (
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
)

# Sections whose value is passed through ``_filter_dataclass_fields`` directly.
_SIMPLE_SECTION_MAP: dict[str, type] = {
    "observability": ObservabilityConfig,
    "access_control": AccessControlConfig,
    "identity": IdentityConfig,
    "defaults": DefaultsConfig,
    "mcp": McpConfig,
    "recall_cache": RecallCacheConfig,
    "tiered_retrieval": TieredRetrievalConfig,
    "curated_retain": CuratedRetainConfig,
    "curated_recall": CuratedRecallConfig,
    "dlp": DlpConfig,
}


def _dict_to_config(data: dict) -> AstrocyteConfig:
    """Convert a flat/nested dict to AstrocyteConfig with nested dataclasses."""
    config = AstrocyteConfig()

    # ── Scalar fields ──
    for field_name in _SCALAR_CONFIG_FIELDS:
        if field_name in data:
            setattr(config, field_name, data[field_name])

    # ── Simple nested sections (filter + construct) ──
    for section, cls in _SIMPLE_SECTION_MAP.items():
        if section in data:
            setattr(config, section, cls(**_filter_dataclass_fields(cls, data[section])))

    # ── Complex nested sections (dedicated parsers) ──
    if "homeostasis" in data:
        config.homeostasis = _parse_homeostasis(data["homeostasis"])

    if "barriers" in data:
        config.barriers = _parse_barriers(data["barriers"])

    if "escalation" in data:
        config.escalation = _parse_escalation(data["escalation"])

    if "signal_quality" in data:
        config.signal_quality = _parse_signal_quality(data["signal_quality"])

    if "recall_authority" in data and isinstance(data["recall_authority"], dict):
        config.recall_authority = _parse_recall_authority(data["recall_authority"])

    if "access_grants" in data and data["access_grants"]:
        config.access_grants = _parse_access_grants(data["access_grants"])

    if "lifecycle" in data:
        config.lifecycle = _parse_lifecycle(data["lifecycle"])

    if "banks" in data and data["banks"]:
        config.banks = _parse_banks(data["banks"])

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
        config.agents = _parse_agents(data["agents"])

    if "deployment" in data and isinstance(data["deployment"], dict):
        config.deployment = _parse_deployment(data["deployment"])

    # ── Scalar fallbacks ──
    if "compliance_profile" in data:
        config.compliance_profile = data["compliance_profile"]

    if "mip_config_path" in data:
        config.mip_config_path = data["mip_config_path"]
    elif "mip" in data and isinstance(data["mip"], str):
        config.mip_config_path = data["mip"]

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
            if st in ("poll", "api_poll"):
                driver = (src.driver or "").strip().lower()
                if driver != "github":
                    raise ConfigError(
                        f"sources.{name}: poll driver {driver!r} is not supported (use github; "
                        "install astrocyte-ingestion-github)"
                    )
                if not src.interval_seconds or int(src.interval_seconds) < 60:
                    raise ConfigError(
                        f"sources.{name}: type poll requires interval_seconds >= 60 "
                        "(GitHub API rate limits)"
                    )
                pr = (src.path or "").strip()
                if not pr or "/" not in pr or pr.count("/") != 1:
                    raise ConfigError(
                        f"sources.{name}: type poll with driver github requires path: owner/repo "
                        f"(got {pr!r})"
                    )
                if not (src.target_bank or "").strip() and not (src.target_bank_template or "").strip():
                    raise ConfigError(
                        f"sources.{name}: type poll requires target_bank or target_bank_template"
                    )
                tok = (src.auth or {}).get("token") if src.auth else None
                if not (str(tok).strip() if tok is not None else ""):
                    raise ConfigError(
                        f"sources.{name}: type poll with driver github requires auth.token "
                        "(GitHub personal access token or fine-grained token)"
                    )
            if st == "stream":
                driver = (src.driver or "redis").strip().lower()
                if driver not in ("redis", "kafka"):
                    raise ConfigError(
                        f"sources.{name}: stream driver {driver!r} is not supported (use redis or kafka)"
                    )
                if not (src.url or "").strip():
                    raise ConfigError(
                        f"sources.{name}: type stream requires url (Redis URL or Kafka bootstrap servers)"
                    )
                if not (src.topic or "").strip():
                    tlabel = "Redis stream key / Kafka topic"
                    raise ConfigError(f"sources.{name}: type stream requires topic ({tlabel})")
                if not (src.consumer_group or "").strip():
                    raise ConfigError(f"sources.{name}: type stream requires consumer_group")
                if not (src.target_bank or "").strip() and not (src.target_bank_template or "").strip():
                    raise ConfigError(
                        f"sources.{name}: type stream requires target_bank or target_bank_template"
                    )
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
            for idx, row in enumerate(bc.access):
                if not isinstance(row, dict):
                    continue
                label = f"banks.{bank_id}.access[{idx}]"
                if "principal" not in row:
                    raise ConfigError(f"{label} missing required key: principal")
                if "permissions" not in row or not isinstance(row["permissions"], list):
                    raise ConfigError(f"{label} missing or invalid 'permissions' (must be a list)")
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

    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    # Substitute environment variables
    raw = _substitute_env_recursive(raw)

    # Check for unresolved env vars — fail on sensitive fields, warn on others
    unresolved = _find_unresolved_env_vars(raw)
    if unresolved:
        import logging

        _cfg_logger = logging.getLogger("astrocyte.config")
        sensitive = [r for r in unresolved if _is_sensitive_field(r)]
        if sensitive:
            raise ConfigError(
                "Unresolved environment variables in sensitive config fields: "
                + "; ".join(sensitive)
            )
        for ref in unresolved:
            _cfg_logger.warning("Unresolved environment variable in config: %s", ref)

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
    if cfg.mip_config_path:
        mip = Path(cfg.mip_config_path)
        if not mip.is_absolute():
            cfg.mip_config_path = str((config_path.parent / mip).resolve())
    validate_astrocyte_config(cfg)
    return cfg
