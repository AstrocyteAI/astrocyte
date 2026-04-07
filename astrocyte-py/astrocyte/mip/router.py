"""MIP router — orchestrates rule_engine → ambiguity detection → intent layer.

See docs/_design/memory-intent-protocol.md for the design specification.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from astrocyte.mip.rule_engine import RuleEngineInput, RuleMatch, evaluate_rules, interpolate_template
from astrocyte.mip.schema import MipConfig
from astrocyte.types import RoutingDecision

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

logger = logging.getLogger("astrocyte.mip")


class MipRouter:
    """Top-level MIP router. Orchestrates rule_engine → escalation → intent layer."""

    def __init__(self, config: MipConfig, llm_provider: LLMProvider | None = None) -> None:
        self._config = config
        self._llm_provider = llm_provider
        self._rules = sorted(config.rules or [], key=lambda r: r.priority)

    def route_sync(self, input_data: RuleEngineInput) -> RoutingDecision | None:
        """Attempt synchronous (mechanical) routing only.

        Returns RoutingDecision if a confident match is found.
        Returns None if escalation to intent layer is needed.
        """
        matches = evaluate_rules(self._rules, input_data)

        if not matches:
            return None if self._should_escalate(matches) else None

        top = matches[0]

        # Override rule — compliance lock, always return
        if top.rule.override:
            return self._apply_action(top, input_data)

        # Check for escalation action
        if top.rule.action.escalate == "mip":
            return None

        # Confident single match
        if len(matches) == 1 and top.confidence >= 0.8:
            return self._apply_action(top, input_data)

        # Multiple matches — potential conflict, check escalation policy
        if len(matches) > 1 and self._should_escalate(matches):
            return None

        if len(matches) > 1:
            # Multiple matches but no escalation policy — use highest priority
            return self._apply_action(top, input_data)

        return self._apply_action(top, input_data)

    def _should_escalate(self, matches: list[RuleMatch]) -> bool:
        """Check escalation conditions from intent_policy.escalate_when."""
        policy = self._config.intent_policy
        if not policy or not policy.escalate_when:
            return True  # Default: escalate when no explicit policy

        for condition in policy.escalate_when:
            if condition.condition == "matched_rules":
                count = len(matches)
                if self._compare(count, condition.operator, condition.value):
                    return True
            elif condition.condition == "confidence":
                if matches:
                    top_confidence = matches[0].confidence
                    if self._compare(top_confidence, condition.operator, condition.value):
                        return True
            elif condition.condition == "conflicting_rules":
                if condition.value and len(matches) > 1:
                    return True
        return False

    @staticmethod
    def _compare(actual: int | float, operator: str, expected: str | int | float | bool) -> bool:
        """Compare a value against a condition."""
        try:
            a = float(actual)
            e = float(expected)
        except (TypeError, ValueError):
            return actual == expected
        if operator == "eq":
            return a == e
        if operator == "lt":
            return a < e
        if operator == "gt":
            return a > e
        if operator == "gte":
            return a >= e
        if operator == "lte":
            return a <= e
        return a == e

    async def route(self, input_data: RuleEngineInput) -> RoutingDecision:
        """Full routing: mechanical rules first, then intent layer if needed.

        1. Evaluate override rules → if match, return immediately (compliance lock)
        2. Evaluate normal rules → if confident match, return
        3. Check escalation conditions
        4. If escalation needed and LLM available, call intent layer
        5. If no LLM, return passthrough decision
        """
        # Try mechanical routing first
        decision = self.route_sync(input_data)
        if decision is not None:
            return decision

        # Need escalation — try intent layer
        if self._llm_provider and self._config.intent_policy:
            from astrocyte.mip.intent import resolve_intent

            return await resolve_intent(
                input_data=input_data,
                intent_policy=self._config.intent_policy,
                available_banks=self._config.banks or [],
                llm_provider=self._llm_provider,
            )

        # No LLM available — passthrough
        return RoutingDecision(resolved_by="passthrough", reasoning="No mechanical match and no LLM available")

    def _apply_action(self, match: RuleMatch, input_data: RuleEngineInput) -> RoutingDecision:
        """Convert a RuleMatch into a RoutingDecision, interpolating templates."""
        action = match.rule.action

        bank_id = interpolate_template(action.bank, input_data) if action.bank else None
        tags = [interpolate_template(t, input_data) for t in action.tags] if action.tags else None

        return RoutingDecision(
            bank_id=bank_id,
            tags=tags,
            retain_policy=action.retain_policy,
            resolved_by="mechanical",
            rule_name=match.rule.name,
            confidence=match.confidence,
        )
