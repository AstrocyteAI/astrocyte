"""MIP config loader — YAML loading, env var substitution, validation."""

from __future__ import annotations

from pathlib import Path

import yaml

from astrocyte.config import _substitute_env_recursive
from astrocyte.errors import ConfigError
from astrocyte.mip.presets import (
    expand_forget_preset,
    expand_preset,
    is_known_forget_preset,
    is_known_preset,
    list_forget_presets,
    list_presets,
)
from astrocyte.mip.schema import (
    ActionSpec,
    BankDefinition,
    ChunkerSpec,
    DedupSpec,
    EscalationCondition,
    ForgetSpec,
    IntentPolicy,
    MatchBlock,
    MatchSpec,
    MipConfig,
    PipelineSpec,
    ReflectSpec,
    RerankSpec,
    RoutingRule,
)

# Recognised sub-keys for the pipeline block. Unknown keys at any level emit
# warnings during load (forward-compat: vocabulary may grow).
_PIPELINE_KEYS = {"version", "preset", "chunker", "dedup", "rerank", "reflect"}
_CHUNKER_KEYS = {"strategy", "max_size", "overlap"}
_DEDUP_KEYS = {"threshold", "action"}
_RERANK_KEYS = {"keyword_weight", "proper_noun_weight"}
_REFLECT_KEYS = {"prompt", "promote_metadata"}
_FORGET_KEYS = {
    "version", "preset", "mode", "audit", "cascade",
    "respect_legal_hold", "min_age_days", "max_per_call",
}
_FORGET_MODES = {"soft", "hard", "tombstone"}
_FORGET_AUDIT = {"none", "recommended", "required"}

# P4: hard cap on metadata fields promoted into reflect prompt
_PROMOTE_METADATA_MAX = 5


def load_mip_config(path: str | Path) -> MipConfig:
    """Load mip.yaml, substitute env vars, validate, return MipConfig."""
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"MIP config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    raw = _substitute_env_recursive(raw)
    return _parse_mip_config(raw)


def _parse_mip_config(data: dict) -> MipConfig:
    """Parse a YAML dict into MipConfig."""
    config = MipConfig(version=data.get("version", "1.0"))

    # Banks
    if "banks" in data and data["banks"]:
        config.banks = [
            BankDefinition(
                id=b["id"],
                description=b.get("description"),
                access=b.get("access"),
                compliance=b.get("compliance"),
            )
            for b in data["banks"]
        ]

    # Rules
    if "rules" in data and data["rules"]:
        config.rules = [_parse_rule(r) for r in data["rules"]]

    # Intent policy
    if "intent_policy" in data and data["intent_policy"]:
        ip = data["intent_policy"]
        escalate_when = None
        if "escalate_when" in ip and ip["escalate_when"]:
            escalate_when = []
            for ew in ip["escalate_when"]:
                if isinstance(ew, dict):
                    for key, val in ew.items():
                        if isinstance(val, dict):
                            for op, v in val.items():
                                escalate_when.append(EscalationCondition(condition=key, operator=op, value=v))
                        else:
                            escalate_when.append(EscalationCondition(condition=key, operator="eq", value=val))

        config.intent_policy = IntentPolicy(
            escalate_when=escalate_when,
            model_context=ip.get("model_context"),
            constraints=ip.get("constraints"),
        )

    errors = _validate_mip_config(config)
    if errors:
        raise ConfigError(f"MIP config validation errors: {'; '.join(errors)}")

    return config


def _parse_rule(data: dict) -> RoutingRule:
    """Parse a single rule dict into RoutingRule."""
    match_data = data.get("match", {})
    action_data = data.get("action", {})
    rule_name = data.get("name", "<unnamed>")

    return RoutingRule(
        name=data["name"],
        priority=data.get("priority", 100),
        match=_parse_match_block(match_data),
        action=_parse_action(action_data, rule_name=rule_name, match_data=match_data),
        override=data.get("override", False),
    )


def _parse_match_block(data: dict) -> MatchBlock:
    """Parse a YAML match block into MatchBlock."""
    all_conditions = None
    any_conditions = None
    none_conditions = None

    if "all" in data:
        all_conditions = [_parse_match_spec(s) for s in data["all"]] if data["all"] else []
    if "any" in data:
        any_conditions = [_parse_match_spec(s) for s in data["any"]] if data["any"] else []
    if "none" in data:
        none_conditions = [_parse_match_spec(s) for s in data["none"]] if data["none"] else []

    # Single-level shorthand: {"content_type": "student_answer", "pii_detected": true}
    if not all_conditions and not any_conditions and not none_conditions:
        specs = []
        for key, val in data.items():
            if key in ("all", "any", "none"):
                continue
            specs.append(_parse_match_spec({key: val}))
        if specs:
            all_conditions = specs

    return MatchBlock(
        all_conditions=all_conditions,
        any_conditions=any_conditions,
        none_conditions=none_conditions,
    )


def _parse_match_spec(data: dict) -> MatchSpec:
    """Parse a single match spec dict into MatchSpec."""
    for field_name, value in data.items():
        if isinstance(value, dict):
            # Operator form: {"metadata.count": {"gte": 5}}
            for op, v in value.items():
                return MatchSpec(field=field_name, operator=op, value=v)
        elif value == "present":
            return MatchSpec(field=field_name, operator="present")
        elif value == "absent":
            return MatchSpec(field=field_name, operator="absent")
        else:
            # Simple equality: {"content_type": "student_answer"}
            return MatchSpec(field=field_name, operator="eq", value=value)
    raise ConfigError("Empty match spec")


def _parse_action(
    data: dict,
    rule_name: str = "<unnamed>",
    match_data: dict | None = None,
) -> ActionSpec:
    """Parse a YAML action block into ActionSpec.

    `rule_name` and `match_data` are used for guardrail diagnostics on the
    optional `pipeline:` sub-block (P2/P4/P5).
    """
    pipeline_data = data.get("pipeline")
    pipeline = (
        _parse_pipeline(pipeline_data, rule_name=rule_name, match_data=match_data or {})
        if pipeline_data is not None
        else None
    )

    forget_data = data.get("forget")
    forget = (
        _parse_forget(forget_data, rule_name=rule_name)
        if forget_data is not None
        else None
    )

    return ActionSpec(
        bank=data.get("bank"),
        tags=data.get("tags"),
        retain_policy=data.get("retain_policy"),
        escalate=data.get("escalate"),
        confidence=data.get("confidence", 1.0),
        pipeline=pipeline,
        forget=forget,
    )


def _parse_forget(data: dict, rule_name: str) -> ForgetSpec:
    """Parse and validate an action.forget sub-block (Phase 4).

    Enforces:
    - P2: ``version`` is required
    - ``mode`` ∈ {soft, hard, tombstone}
    - ``audit`` ∈ {none, recommended, required}
    - ``min_age_days``, ``max_per_call`` non-negative ints
    - ``mode: hard`` requires ``audit: required`` (compliance discipline)
    - Unknown preset names error with a list of valid presets
    """
    if not isinstance(data, dict):
        raise ConfigError(f"Rule '{rule_name}': forget must be a mapping")

    _warn_unknown_keys(data, _FORGET_KEYS, f"rule '{rule_name}' forget")

    version = data.get("version")
    if version is None:
        raise ConfigError(
            f"Rule '{rule_name}': forget.version is required when forget block is set"
        )
    if not isinstance(version, int):
        raise ConfigError(
            f"Rule '{rule_name}': forget.version must be an integer (got {type(version).__name__})"
        )

    preset = data.get("preset")
    if preset is not None and not is_known_forget_preset(preset):
        raise ConfigError(
            f"Rule '{rule_name}': unknown forget preset '{preset}' "
            f"(known: {', '.join(list_forget_presets())})"
        )

    mode = data.get("mode")
    if mode is not None and mode not in _FORGET_MODES:
        raise ConfigError(
            f"Rule '{rule_name}': forget.mode must be one of {sorted(_FORGET_MODES)} (got {mode!r})"
        )

    audit = data.get("audit")
    if audit is not None and audit not in _FORGET_AUDIT:
        raise ConfigError(
            f"Rule '{rule_name}': forget.audit must be one of {sorted(_FORGET_AUDIT)} (got {audit!r})"
        )

    min_age = data.get("min_age_days")
    if min_age is not None and (not isinstance(min_age, int) or min_age < 0):
        raise ConfigError(
            f"Rule '{rule_name}': forget.min_age_days must be a non-negative int"
        )

    max_per_call = data.get("max_per_call")
    if max_per_call is not None and (not isinstance(max_per_call, int) or max_per_call <= 0):
        raise ConfigError(
            f"Rule '{rule_name}': forget.max_per_call must be a positive int"
        )

    spec = ForgetSpec(
        version=version,
        preset=preset,
        mode=mode,
        audit=audit,
        cascade=data.get("cascade"),
        respect_legal_hold=data.get("respect_legal_hold"),
        min_age_days=min_age,
        max_per_call=max_per_call,
    )
    resolved = expand_forget_preset(spec)

    # Compliance discipline: hard delete demands audit. Check after preset
    # expansion so the gdpr preset (mode=hard, audit=required) passes cleanly.
    if resolved.mode == "hard" and resolved.audit != "required":
        raise ConfigError(
            f"Rule '{rule_name}': forget.mode='hard' requires forget.audit='required'"
        )

    return resolved


def _parse_pipeline(
    data: dict,
    rule_name: str,
    match_data: dict,
) -> PipelineSpec:
    """Parse and validate a pipeline action sub-block.

    Enforces guardrails:
    - P2: version is required when any pipeline field is set
    - P4: reflect.promote_metadata capped at 5 fields
    - P5: pipeline fields require content_type in match block

    Unknown keys at any level emit warnings (forward-compatible vocabulary).
    Presets are expanded at load time so downstream code never sees `preset`.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"Rule '{rule_name}': pipeline must be a mapping")

    _warn_unknown_keys(data, _PIPELINE_KEYS, f"rule '{rule_name}' pipeline")

    # P2: version required
    version = data.get("version")
    if version is None:
        raise ConfigError(
            f"Rule '{rule_name}': pipeline.version is required when pipeline block is set"
        )
    if not isinstance(version, int):
        raise ConfigError(
            f"Rule '{rule_name}': pipeline.version must be an integer (got {type(version).__name__})"
        )

    # P5: content_type must be referenced in match block
    if not _match_references_content_type(match_data):
        raise ConfigError(
            f"Rule '{rule_name}': pipeline fields require 'content_type' in the match block"
        )

    preset = data.get("preset")
    if preset is not None and not is_known_preset(preset):
        raise ConfigError(
            f"Rule '{rule_name}': unknown preset '{preset}' "
            f"(known: {', '.join(list_presets())})"
        )

    spec = PipelineSpec(
        version=version,
        preset=preset,
        chunker=_parse_chunker(data.get("chunker"), rule_name),
        dedup=_parse_dedup(data.get("dedup"), rule_name),
        rerank=_parse_rerank(data.get("rerank"), rule_name),
        reflect=_parse_reflect(data.get("reflect"), rule_name),
    )

    return expand_preset(spec)


def _parse_chunker(data: dict | None, rule_name: str) -> ChunkerSpec | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"Rule '{rule_name}': pipeline.chunker must be a mapping")
    _warn_unknown_keys(data, _CHUNKER_KEYS, f"rule '{rule_name}' pipeline.chunker")
    return ChunkerSpec(
        strategy=data.get("strategy"),
        max_size=data.get("max_size"),
        overlap=data.get("overlap"),
    )


def _parse_dedup(data: dict | None, rule_name: str) -> DedupSpec | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"Rule '{rule_name}': pipeline.dedup must be a mapping")
    _warn_unknown_keys(data, _DEDUP_KEYS, f"rule '{rule_name}' pipeline.dedup")
    return DedupSpec(
        threshold=data.get("threshold"),
        action=data.get("action"),
    )


def _parse_rerank(data: dict | None, rule_name: str) -> RerankSpec | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"Rule '{rule_name}': pipeline.rerank must be a mapping")
    _warn_unknown_keys(data, _RERANK_KEYS, f"rule '{rule_name}' pipeline.rerank")
    return RerankSpec(
        keyword_weight=data.get("keyword_weight"),
        proper_noun_weight=data.get("proper_noun_weight"),
    )


def _parse_reflect(data: dict | None, rule_name: str) -> ReflectSpec | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"Rule '{rule_name}': pipeline.reflect must be a mapping")
    _warn_unknown_keys(data, _REFLECT_KEYS, f"rule '{rule_name}' pipeline.reflect")
    promote = data.get("promote_metadata")
    if promote is not None:
        if not isinstance(promote, list):
            raise ConfigError(
                f"Rule '{rule_name}': pipeline.reflect.promote_metadata must be a list"
            )
        # P4: hard cap
        if len(promote) > _PROMOTE_METADATA_MAX:
            raise ConfigError(
                f"Rule '{rule_name}': pipeline.reflect.promote_metadata "
                f"capped at {_PROMOTE_METADATA_MAX} fields (got {len(promote)})"
            )
    return ReflectSpec(
        prompt=data.get("prompt"),
        promote_metadata=promote,
    )


def _match_references_content_type(match_data: dict) -> bool:
    """True if the match block references content_type at any level."""
    if not match_data:
        return False
    if "content_type" in match_data:
        return True
    for key in ("all", "any", "none"):
        block = match_data.get(key) or []
        for spec in block:
            if isinstance(spec, dict) and "content_type" in spec:
                return True
    return False


def _warn_unknown_keys(data: dict, known: set[str], context: str) -> None:
    import warnings

    unknown = set(data.keys()) - known
    if unknown:
        warnings.warn(
            f"MIP loader: unknown keys in {context}: {sorted(unknown)}",
            stacklevel=3,
        )


def _validate_mip_config(config: MipConfig) -> list[str]:
    """Validate internal consistency. Returns list of error messages."""
    errors: list[str] = []

    if config.rules:
        names = [r.name for r in config.rules]
        if len(names) != len(set(names)):
            errors.append("Duplicate rule names found")

        for rule in config.rules:
            if not rule.name:
                errors.append("Rule missing 'name'")
            if rule.action.escalate and rule.override:
                errors.append(f"Rule '{rule.name}' cannot have both override=true and escalate=mip")

    return errors
