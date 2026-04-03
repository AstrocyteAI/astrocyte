"""Astrocytes configuration — YAML loading, profile resolution, env var substitution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from astrocytes.errors import ConfigError

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
class BankConfig:
    """Per-bank override settings."""

    profile: str | None = None
    access: list[dict[str, str | list[str]]] | None = None
    homeostasis: HomeostasisConfig | None = None
    barriers: BarrierConfig | None = None
    signal_quality: SignalQualityConfig | None = None


@dataclass
class AstrocyteConfig:
    """Top-level Astrocytes configuration."""

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

    # Per-bank overrides
    banks: dict[str, BankConfig] | None = None


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


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base. Override values win."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


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

    return config


def load_config(path: str | Path) -> AstrocyteConfig:
    """Load Astrocytes configuration from a YAML file.

    Resolution order: profile defaults → user config → per-bank overrides.
    Environment variables are substituted (${VAR_NAME}).
    """
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    # Substitute environment variables
    raw = _substitute_env_recursive(raw)

    # Load and merge profile if specified
    profile_name = raw.get("profile")
    if profile_name:
        profile_data = _load_profile(profile_name)
        raw = _deep_merge(profile_data, raw)

    return _dict_to_config(raw)
