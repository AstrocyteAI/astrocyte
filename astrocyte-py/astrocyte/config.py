"""Astrocyte configuration — YAML loading, profile resolution, env var substitution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

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
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)

    # MCP
    mcp: McpConfig = field(default_factory=McpConfig)

    # Phase 2 innovations
    recall_cache: RecallCacheConfig = field(default_factory=RecallCacheConfig)
    tiered_retrieval: TieredRetrievalConfig = field(default_factory=TieredRetrievalConfig)
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
            rate_limits=RateLimitConfig(**{k: v for k, v in rl.items() if v is not None}),
            quotas=QuotaConfig(**{k: v for k, v in q.items() if v is not None}),
        )

    if "barriers" in data:
        b = data["barriers"]
        pii_data = b.get("pii", {})
        val_data = b.get("validation", {})
        meta_data = b.get("metadata", {})
        config.barriers = BarrierConfig(
            pii=PiiConfig(**{k: v for k, v in pii_data.items()}),
            validation=ValidationConfig(**{k: v for k, v in val_data.items()}),
            metadata=MetadataSanitizationConfig(**{k: v for k, v in meta_data.items()}),
        )

    if "escalation" in data:
        e = data["escalation"]
        cb = e.get("circuit_breaker", {})
        config.escalation = EscalationConfig(
            circuit_breaker=CircuitBreakerConfig(**{k: v for k, v in cb.items()}),
            degraded_mode=e.get("degraded_mode", "empty_recall"),
        )

    if "observability" in data:
        config.observability = ObservabilityConfig(**data["observability"])

    if "access_control" in data:
        config.access_control = AccessControlConfig(**data["access_control"])

    if "defaults" in data:
        config.defaults = DefaultsConfig(**data["defaults"])

    if "mcp" in data:
        config.mcp = McpConfig(**data["mcp"])

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
        config.tiered_retrieval = TieredRetrievalConfig(**_filter_dataclass_fields(TieredRetrievalConfig, data["tiered_retrieval"]))

    if "curated_retain" in data:
        config.curated_retain = CuratedRetainConfig(**_filter_dataclass_fields(CuratedRetainConfig, data["curated_retain"]))

    if "curated_recall" in data:
        config.curated_recall = CuratedRecallConfig(**_filter_dataclass_fields(CuratedRecallConfig, data["curated_recall"]))

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
                    metadata=MetadataSanitizationConfig(**_filter_dataclass_fields(MetadataSanitizationConfig, meta_data)),
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

    return config


def access_grants_for_astrocyte(config: AstrocyteConfig) -> list[AccessGrant]:
    """Flatten ``access_grants`` and ``banks.*.access`` into one list for ``Astrocyte.set_access_grants``."""
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
    return out


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

    return _dict_to_config(merged)
