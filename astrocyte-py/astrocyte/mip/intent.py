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

    System message contains routing instructions (trusted).
    User message wraps untrusted content in XML delimiters.
    Falls back to passthrough on failure.
    """
    max_tokens = 200
    if intent_policy.constraints and "max_tokens" in intent_policy.constraints:
        max_tokens = int(intent_policy.constraints["max_tokens"])

    system_msg = _build_system_message(intent_policy, available_banks)
    user_msg = _build_user_message(input_data)

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


def _build_system_message(
    intent_policy: IntentPolicy,
    available_banks: list[BankDefinition],
) -> str:
    """Build the system message with routing instructions (trusted content only)."""
    banks_str = ", ".join(b.id for b in available_banks) if available_banks else "(none defined)"

    if intent_policy.model_context:
        base = intent_policy.model_context.replace("{banks}", banks_str)
    else:
        base = (
            f"You are a memory routing agent. Route content to the correct bank and apply tags.\n"
            f"Available banks: {banks_str}\n"
            f"Never override compliance rules."
        )

    return f"""{base}

The user message contains the content to route inside <content> XML tags, along with metadata.
Respond with a JSON object:
{{"bank_id": "...", "tags": ["..."], "retain_policy": "default", "reasoning": "..."}}"""


def _build_user_message(input_data: RuleEngineInput) -> str:
    """Build the user message with untrusted content wrapped in XML delimiters."""
    tags_str = ", ".join(input_data.tags) if input_data.tags else "(none)"
    return f"""<content>
{input_data.content[:500]}
</content>

Content type: {input_data.content_type or "text"}
Source: {input_data.source or "unknown"}
Tags: {tags_str}
PII detected: {input_data.pii_detected}"""


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
