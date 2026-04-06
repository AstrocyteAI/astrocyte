"""LLM-based PII scanner — contextual PII detection via LLM.

Detects PII that regex and NER miss: medical records, contextual references
("my mother's maiden name is Smith"), and implicit PII patterns.

Async — requires LLM API call.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from astrocyte.types import Message, PiiMatch

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

logger = logging.getLogger("astrocyte.pii")

_LLM_PII_PROMPT = """Analyze this text for personally identifiable information (PII). Return a JSON array of detected PII items.

For each PII item found, return:
- "type": the PII category (name, email, phone, address, ssn, credit_card, medical_record, date_of_birth, national_id, passport, financial_account)
- "text": the exact matched text
- "start": character offset where the PII starts
- "end": character offset where the PII ends

If no PII is found, return an empty array: []

Text to analyze:
{text}

Respond with ONLY the JSON array, no other text."""


class LlmPiiScanner:
    """LLM-based contextual PII detection.

    Async — requires LLM provider for inference.
    Falls back to empty list on any failure.
    """

    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm = llm_provider

    async def scan(self, text: str) -> list[PiiMatch]:
        """Ask LLM to identify PII with positions. Returns matches."""
        prompt = _LLM_PII_PROMPT.replace("{text}", text[:2000])  # Cap input length

        try:
            completion = await self._llm.complete(
                messages=[Message(role="user", content=prompt)],
                max_tokens=500,
                temperature=0,
            )
            return _parse_llm_response(completion.text, text)
        except Exception:
            logger.warning("LLM PII scan failed, returning empty matches")
            return []


# Replacement map for LLM-detected types
_TYPE_REPLACEMENTS: dict[str, str] = {
    "name": "[NAME_REDACTED]",
    "email": "[EMAIL_REDACTED]",
    "phone": "[PHONE_REDACTED]",
    "address": "[ADDRESS_REDACTED]",
    "ssn": "[SSN_REDACTED]",
    "credit_card": "[CC_REDACTED]",
    "medical_record": "[MEDICAL_REDACTED]",
    "date_of_birth": "[DOB_REDACTED]",
    "national_id": "[NATIONAL_ID_REDACTED]",
    "passport": "[PASSPORT_REDACTED]",
    "financial_account": "[ACCOUNT_REDACTED]",
}


def _parse_llm_response(response: str, original_text: str) -> list[PiiMatch]:
    """Parse LLM JSON response into PiiMatch list. Graceful fallback."""
    try:
        text = response.strip()
        # Extract from code block if present
        if "```" in text:
            start = text.index("```") + 3
            if text[start:].startswith("json"):
                start += 4
            end = text.index("```", start)
            text = text[start:end].strip()

        items = json.loads(text)
        if not isinstance(items, list):
            return []

        matches: list[PiiMatch] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            pii_type = item.get("type", "unknown")
            matched_text = item.get("text", "")
            start = item.get("start")
            end = item.get("end")

            # Validate positions — LLMs sometimes get offsets wrong
            if start is not None and end is not None:
                start = int(start)
                end = int(end)
            elif matched_text:
                # Try to find the text in original
                idx = original_text.find(matched_text)
                if idx >= 0:
                    start = idx
                    end = idx + len(matched_text)
                else:
                    continue  # Can't locate — skip

            if start is None or end is None:
                continue

            replacement = _TYPE_REPLACEMENTS.get(pii_type, f"[{pii_type.upper()}_REDACTED]")
            matches.append(
                PiiMatch(
                    pii_type=pii_type,
                    start=start,
                    end=end,
                    matched_text=matched_text,
                    replacement=replacement,
                )
            )

        return matches
    except (json.JSONDecodeError, ValueError, KeyError):
        logger.warning("Failed to parse LLM PII response")
        return []
