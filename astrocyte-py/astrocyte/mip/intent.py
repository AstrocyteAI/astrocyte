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
    prompt = _build_intent_prompt(input_data, intent_policy, available_banks)

    max_tokens = 200
    if intent_policy.constraints and "max_tokens" in intent_policy.constraints:
        max_tokens = int(intent_policy.constraints["max_tokens"])

    try:
        system_msg, user_msg = _split_prompt(prompt, input_data)
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


def _build_intent_prompt(
    input_data: RuleEngineInput,
    intent_policy: IntentPolicy,
    available_banks: list[BankDefinition],
) -> str:
    """Build the prompt for the LLM intent layer."""
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

    prompt = f"""{base}

Content to route:
{input_data.content[:500]}

Content type: {input_data.content_type or 'text'}
Source: {input_data.source or 'unknown'}
PII detected: {input_data.pii_detected}

Respond with JSON:
{{"bank_id": "...", "tags": ["..."], "retain_policy": "default", "reasoning": "..."}}"""

    return prompt


def _split_prompt(prompt: str, input_data: RuleEngineInput) -> tuple[str, str]:
    """Split prompt into system + user messages to mitigate prompt injection.

    System message contains routing instructions. User message wraps
    untrusted content in XML delimiters.
    """
    # Everything before "Content to route:" is the system instruction
    marker = "Content to route:"
    if marker in prompt:
        idx = prompt.index(marker)
        system = prompt[:idx].strip()
        user = f"<content>\n{input_data.content[:500]}\n</content>\n\nContent type: {input_data.content_type or 'text'}\nSource: {input_data.source or 'unknown'}\nPII detected: {input_data.pii_detected}\n\nRespond with JSON:\n{{\"bank_id\": \"...\", \"tags\": [\"...\"], \"retain_policy\": \"default\", \"reasoning\": \"...\"}}"
        return system, user
    return prompt, ""


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
