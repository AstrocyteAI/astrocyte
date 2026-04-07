"""MIP config loader — YAML loading, env var substitution, validation."""

from __future__ import annotations

from pathlib import Path

import yaml

from astrocyte.config import _substitute_env_recursive
from astrocyte.errors import ConfigError
from astrocyte.mip.schema import (
    ActionSpec,
    BankDefinition,
    EscalationCondition,
    IntentPolicy,
    MatchBlock,
    MatchSpec,
    MipConfig,
    RoutingRule,
)


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

    return RoutingRule(
        name=data["name"],
        priority=data.get("priority", 100),
        match=_parse_match_block(match_data),
        action=_parse_action(action_data),
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


def _parse_action(data: dict) -> ActionSpec:
    """Parse a YAML action block into ActionSpec."""
    return ActionSpec(
        bank=data.get("bank"),
        tags=data.get("tags"),
        retain_policy=data.get("retain_policy"),
        escalate=data.get("escalate"),
        confidence=data.get("confidence", 1.0),
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
