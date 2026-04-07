"""MIP intent layer — LLM-based routing when mechanical rules can't resolve.

Async — requires LLM call. Follows the pattern of pipeline/curated_retain.py.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from astrocyte.mip.schema import BankDefinition, IntentPolicy
from astrocyte.types import Message, RoutingDecision

if TYPE_CHECKING:
    from astrocyte.mip.rule_engine import RuleEngineInput
    from astrocyte.provider import LLMProvider

logger = logging.getLogger("astrocyte.mip")


async def resolve_intent(
    input_data: RuleEngineInput,
    intent_policy: IntentPolicy,
    available_banks: list[BankDefinition],
    llm_provider: LLMProvider,
    *,
    model: str | None = None,
) -> RoutingDecision:
    """Ask the LLM to route content when mechanical rules cannot resolve.

    Falls back to passthrough on failure.
    """
    system_msg = _build_system_prompt(intent_policy, available_banks, input_data)
    user_msg = _build_user_message(input_data)

    max_tokens = 200
    if intent_policy.constraints and "max_tokens" in intent_policy.constraints:
        max_tokens = int(intent_policy.constraints["max_tokens"])

    try:
        completion = await llm_provider.complete(
            messages=[
                Message(role="system", content=system_msg),
                Message(role="user", content=user_msg),
            ],
            max_tokens=max_tokens,
            temperature=0,
        )
        return _parse_intent_response(completion.text)
    except Exception:
        logger.warning("MIP intent layer failed, falling back to passthrough")
        return RoutingDecision(resolved_by="passthrough", reasoning="Intent layer LLM call failed")


def _build_system_prompt(
    intent_policy: IntentPolicy,
    available_banks: list[BankDefinition],
    input_data: RuleEngineInput,
) -> str:
    """Build the system prompt for the LLM intent layer.

    Contains only trusted routing instructions — no user content.
    """
    banks_str = ", ".join(b.id for b in available_banks) if available_banks else "(none defined)"
    tags_str = ", ".join(input_data.tags) if input_data.tags else "(none)"

    if intent_policy.model_context:
        base = intent_policy.model_context.replace("{banks}", banks_str).replace("{tags}", tags_str)
    else:
        base = (
            f"You are a memory routing agent. Route content to the correct bank and apply tags.\n"
            f"Available banks: {banks_str}\n"
            f"Current tags: {tags_str}\n"
            f"Never override compliance rules."
        )

    return (
        f"{base}\n\n"
        f"The user message contains content wrapped in <content> XML tags. "
        f"Route it to the appropriate bank based on content type, source, and PII status.\n\n"
        f"Respond with JSON:\n"
        f'{{\"bank_id\": \"...\", \"tags\": [\"...\"], \"retain_policy\": \"default\", \"reasoning\": \"...\"}}'
    )


def _build_user_message(input_data: RuleEngineInput) -> str:
    """Build the user message with untrusted content wrapped in XML delimiters."""
    return (
        f"<content>\n{input_data.content[:500]}\n</content>\n\n"
        f"Content type: {input_data.content_type or 'text'}\n"
        f"Source: {input_data.source or 'unknown'}\n"
        f"PII detected: {input_data.pii_detected}"
    )


def _parse_intent_response(response: str) -> RoutingDecision:
    """Parse LLM JSON response into RoutingDecision. Graceful fallback."""
    try:
        text = response.strip()
        # Extract from code block if present
        if "```" in text:
            start = text.index("```") + 3
            if text[start:].startswith("json"):
                start += 4
            end = text.index("```", start)
            text = text[start:end].strip()

        data = json.loads(text)
        return RoutingDecision(
            bank_id=data.get("bank_id"),
            tags=data.get("tags"),
            retain_policy=data.get("retain_policy"),
            resolved_by="intent",
            confidence=data.get("confidence", 0.8),
            reasoning=data.get("reasoning"),
        )
    except (json.JSONDecodeError, ValueError, KeyError):
        logger.warning("Failed to parse MIP intent response, falling back to passthrough")
        return RoutingDecision(resolved_by="passthrough", reasoning="Failed to parse intent response")
