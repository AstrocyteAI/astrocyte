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
            return None

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

        # Multiple matches — potential conflict
        if len(matches) > 1:
            return None

        return self._apply_action(top, input_data)

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
