"""MIP router — orchestrates rule_engine → ambiguity detection → intent layer.

See docs/_design/memory-intent-protocol.md for the design specification.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from astrocyte.errors import MipRoutingError
from astrocyte.mip.rule_engine import (
    RuleEngineInput,
    RuleMatch,
    evaluate_match_block,
    evaluate_rules,
    interpolate_template,
)
from astrocyte.mip.schema import ForgetSpec, MipConfig, PipelineSpec, RoutingRule
from astrocyte.types import RoutingDecision

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

logger = logging.getLogger("astrocyte.mip")


_TEMPLATE_PLACEHOLDER = re.compile(r"\{[^}]+\}")


def _bank_matches(template: str, bank_id: str) -> bool:
    """Whether a templated bank pattern (``"student-{id}"``) matches a concrete bank_id.

    Each ``{...}`` placeholder is treated as a non-greedy ``.+?`` wildcard;
    surrounding literal text is regex-escaped.
    """
    if "{" not in template:
        return template == bank_id
    parts = _TEMPLATE_PLACEHOLDER.split(template)
    pattern = "^" + ".+?".join(re.escape(p) for p in parts) + "$"
    return re.match(pattern, bank_id) is not None


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
        # Phase 5 — filter rules outside their activation window.
        eligible = [r for r in self._rules if _is_active(r)]

        # Phase 5 — shadow rules: evaluate them off to the side, log the match
        # for observability, then exclude them from real routing.
        shadow_rules = [r for r in eligible if r.shadow]
        live_rules = [r for r in eligible if not r.shadow]
        for rule in shadow_rules:
            if evaluate_match_block(rule.match, input_data):
                logger.info(
                    "mip shadow match (no action taken): rule=%s priority=%d tags=%s",
                    rule.name, rule.priority, rule.observability_tags,
                )

        matches = evaluate_rules(live_rules, input_data)

        if not matches:
            return None

        # Phase 5 — tie-breaking when multiple non-override rules match at the
        # same top priority. Override rules already short-circuit in
        # evaluate_rules (returns a single match). When a tie is resolved
        # explicitly by tie_breaker, bypass escalation — the author has
        # declared deterministic intent for the priority collision.
        top_priority = matches[0].rule.priority
        tied = [m for m in matches if m.rule.priority == top_priority]
        if len(tied) > 1 and not matches[0].rule.override:
            top = self._resolve_top(matches)
            return self._apply_action(top, input_data)
        top = matches[0]

        # Override rule — compliance lock, always return
        if top.rule.override:
            return self._apply_action(top, input_data)

        # Check for escalation action
        if top.rule.action.escalate == "mip":
            return None

        # Confident single match — accept
        if len(matches) == 1 and top.confidence >= 0.8:
            return self._apply_action(top, input_data)

        # Low confidence or multiple matches — check escalation policy
        if self._should_escalate(matches):
            return None

        # Escalation policy says don't escalate — use highest priority match
        return self._apply_action(top, input_data)

    def _resolve_top(self, matches: list[RuleMatch]) -> RuleMatch:
        """Apply tie_breaker policy when multiple matches share the top priority.

        ``matches`` is already sorted ascending by priority.
        """
        top_priority = matches[0].rule.priority
        tied = [m for m in matches if m.rule.priority == top_priority]
        if len(tied) <= 1:
            return tied[0]

        policy = self._config.tie_breaker
        if policy == "first":
            return tied[0]
        if policy == "error":
            names = ", ".join(m.rule.name for m in tied)
            raise MipRoutingError(
                f"MIP tie_breaker=error: {len(tied)} rules matched at priority "
                f"{top_priority}: {names}"
            )
        if policy == "most_specific":
            return max(tied, key=lambda m: _condition_count(m.rule))
        return tied[0]  # defensive fallback

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

    def resolve_forget_for_bank(self, bank_id: str) -> ForgetSpec | None:
        """Resolve the highest-priority rule's ForgetSpec targeting ``bank_id`` (Phase 4).

        At forget time the original RetainRequest context is gone, but forget
        still needs to know the policy (mode/audit/legal_hold/min_age/max_per_call)
        configured for the bank being purged. This walks rules in priority order
        and returns the first ``action.forget`` whose ``action.bank`` resolves
        to ``bank_id``. Bank templates (``"student-{id}"``) match concrete IDs.
        """
        for rule in self._rules:
            if rule.action.forget is None:
                continue
            template = rule.action.bank
            if not template:
                continue
            if _bank_matches(template, bank_id):
                return rule.action.forget
        return None

    def resolve_pipeline_for_bank(self, bank_id: str) -> PipelineSpec | None:
        """Resolve the highest-priority rule's PipelineSpec targeting ``bank_id`` (P3).

        At recall time the original RetainRequest context (content, metadata)
        is gone, but recall still needs to know the rerank/reflect overrides
        configured for the bank being read. This walks rules in priority order
        and returns the first ``action.pipeline`` whose ``action.bank``
        resolves to ``bank_id``.

        ``action.bank`` may contain ``{...}`` template placeholders. Each
        placeholder is treated as a wildcard for matching purposes
        (``"student-{id}"`` matches ``"student-42"``, ``"student-foo"``).
        """
        for rule in self._rules:
            if rule.action.pipeline is None:
                continue
            template = rule.action.bank
            if not template:
                continue
            if _bank_matches(template, bank_id):
                return rule.action.pipeline
        return None

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
            pipeline=action.pipeline,
            forget=action.forget,
            observability_tags=match.rule.observability_tags,
        )


# ---------------------------------------------------------------------------
# Phase 5 helpers
# ---------------------------------------------------------------------------


def _is_active(rule: RoutingRule) -> bool:
    """Whether ``rule`` is within its ``active_from``/``active_until`` window."""
    if rule.active_from is None and rule.active_until is None:
        return True
    now = datetime.now(timezone.utc)
    if rule.active_from is not None and now < rule.active_from:
        return False
    if rule.active_until is not None and now > rule.active_until:
        return False
    return True


def _condition_count(rule: RoutingRule) -> int:
    """Count match conditions (used by tie_breaker=most_specific)."""
    block = rule.match
    count = 0
    if block.all_conditions:
        count += len(block.all_conditions)
    if block.any_conditions:
        count += len(block.any_conditions)
    if block.none_conditions:
        count += len(block.none_conditions)
    return count
