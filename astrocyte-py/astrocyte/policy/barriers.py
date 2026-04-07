"""Barrier policies — PII scanning, content validation, metadata sanitization.

All functions are sync (Rust migration candidates) except scan_async/apply_async.
See docs/_design/policy-layer.md section 2 and docs/_design/data-governance.md.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from astrocyte.errors import PiiRejected
from astrocyte.types import Metadata, PiiMatch

if TYPE_CHECKING:
    from astrocyte.policy.llm_scanner import LlmPiiScanner
    from astrocyte.policy.ner_scanner import NerPiiScanner

logger = logging.getLogger("astrocyte.pii")

# ---------------------------------------------------------------------------
# PII regex patterns — global
# ---------------------------------------------------------------------------

_PII_PATTERNS: dict[str, tuple[re.Pattern[str], str]] = {
    "email": (
        re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
        "[EMAIL_REDACTED]",
    ),
    "phone": (
        re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"),
        "[PHONE_REDACTED]",
    ),
    "ssn": (
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "[SSN_REDACTED]",
    ),
    "credit_card": (
        re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
        "[CC_REDACTED]",
    ),
    "ip_address": (
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
            r"|(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
        ),
        "[IP_REDACTED]",
    ),
    # Global: date of birth (with context words)
    "date_of_birth": (
        re.compile(
            r"(?i)(?:born|dob|date\s+of\s+birth|birthday)[:\s]+(\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})"
        ),
        "[DOB_REDACTED]",
    ),
    # Global: IBAN
    "iban": (
        re.compile(r"\b[A-Z]{2}\d{2}\s?[A-Z0-9]{4}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b"),
        "[IBAN_REDACTED]",
    ),
}

# ---------------------------------------------------------------------------
# Country-specific patterns
# ---------------------------------------------------------------------------

_COUNTRY_PATTERNS: dict[str, dict[str, tuple[re.Pattern[str], str]]] = {
    # ── Singapore ──
    "SG": {
        "nric": (
            re.compile(r"\b[STFGM]\d{7}[A-Z]\b"),
            "[NRIC_REDACTED]",
        ),
        "sg_phone": (
            re.compile(r"\+65\s?\d{4}\s?\d{4}\b"),
            "[PHONE_REDACTED]",
        ),
    },
    # ── India ──
    "IN": {
        "aadhaar": (
            # Aadhaar: 12 digits, first digit 2-9 (never starts with 0 or 1)
            re.compile(r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b"),
            "[AADHAAR_REDACTED]",
        ),
        "pan": (
            re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
            "[PAN_REDACTED]",
        ),
        "in_phone": (
            re.compile(r"\+91\s?\d{5}\s?\d{5}\b"),
            "[PHONE_REDACTED]",
        ),
    },
    # ── United States ──
    "US": {
        "us_passport": (
            # US passport: letter prefix (optional since 2021) + 8-9 digits
            re.compile(r"\b[A-Z]?\d{8,9}\b"),
            "[PASSPORT_REDACTED]",
        ),
    },
    # ── United Kingdom ──
    "UK": {
        "uk_nino": (
            re.compile(r"\b[A-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b"),
            "[NINO_REDACTED]",
        ),
        "uk_nhs": (
            re.compile(r"\b\d{3}\s?\d{3}\s?\d{4}\b"),
            "[NHS_REDACTED]",
        ),
        "uk_phone": (
            re.compile(r"(?:\+44\s?\d{4}\s?\d{6}|0\d{4}\s?\d{6})\b"),
            "[PHONE_REDACTED]",
        ),
    },
    # ── EU: Germany ──
    "DE": {
        "de_personalausweis": (
            # German ID: letter + digit + 8 alphanum + check digit (structured, not generic 10-char)
            re.compile(r"\b[CFGHJKLMNPRTVWXYZ]\d[A-Z0-9]{6}\d\b"),
            "[DE_ID_REDACTED]",
        ),
    },
    # ── EU: France ──
    "FR": {
        "fr_insee": (
            re.compile(r"\b[12]\s?\d{2}\s?\d{2}\s?\d{2}\s?\d{3}\s?\d{3}\s?\d{2}\b"),
            "[FR_SSN_REDACTED]",
        ),
    },
    # ── EU: Italy ──
    "IT": {
        "it_codice_fiscale": (
            re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b"),
            "[IT_CF_REDACTED]",
        ),
    },
    # ── EU: Spain ──
    "ES": {
        "es_dni": (
            re.compile(r"\b\d{8}[A-Z]\b"),
            "[ES_DNI_REDACTED]",
        ),
        "es_nie": (
            re.compile(r"\b[XYZ]\d{7}[A-Z]\b"),
            "[ES_NIE_REDACTED]",
        ),
    },
    # ── Australia ──
    "AU": {
        "au_tfn": (
            re.compile(r"\b\d{3}\s?\d{3}\s?\d{2,3}\b"),
            "[TFN_REDACTED]",
        ),
        "au_medicare": (
            re.compile(r"\b\d{4}\s?\d{5}\s?\d{1,2}\b"),
            "[MEDICARE_REDACTED]",
        ),
        "au_phone": (
            re.compile(r"\+61\s?\d\s?\d{4}\s?\d{4}\b"),
            "[PHONE_REDACTED]",
        ),
    },
    # ── Canada ──
    "CA": {
        "ca_sin": (
            re.compile(r"\b\d{3}\s?\d{3}\s?\d{3}\b"),
            "[SIN_REDACTED]",
        ),
    },
    # ── Japan ──
    "JP": {
        "jp_my_number": (
            # My Number: 12 digits, context-aware to avoid Aadhaar overlap
            re.compile(r"(?i)(?:my\s*number|マイナンバー)[:\s]+(\d{4}\s?\d{4}\s?\d{4})\b"),
            "[MY_NUMBER_REDACTED]",
        ),
        "jp_phone": (
            re.compile(r"\+81\s?\d{1,4}\s?\d{1,4}\s?\d{4}\b"),
            "[PHONE_REDACTED]",
        ),
    },
    # ── China ──
    "CN": {
        "cn_resident_id": (
            re.compile(r"\b\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b"),
            "[CN_ID_REDACTED]",
        ),
        "cn_phone": (
            re.compile(r"\+86\s?1\d{2}\s?\d{4}\s?\d{4}\b"),
            "[PHONE_REDACTED]",
        ),
    },
}


def _luhn_check(number: str) -> bool:
    """Validate credit card or SIN number with Luhn algorithm."""
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 9 or len(digits) > 19:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# ---------------------------------------------------------------------------
# PII Scanner
# ---------------------------------------------------------------------------


class PiiScanner:
    """PII detection with multiple modes: regex, NER, LLM, rules_then_llm.

    Sync scan() works for regex and NER modes.
    Async scan_async()/apply_async() required for LLM and rules_then_llm modes.
    """

    def __init__(
        self,
        mode: str = "regex",
        action: str = "redact",
        custom_patterns: dict[str, tuple[str, str]] | None = None,
        countries: list[str] | None = None,
        type_overrides: dict[str, dict[str, str]] | None = None,
        llm_provider: object | None = None,
        ner_model: str = "en_core_web_sm",
    ) -> None:
        self.mode = mode
        self.action = action  # "redact" | "reject" | "warn"
        self._type_overrides = type_overrides or {}

        # Build pattern dict: global + country-specific
        self._patterns = dict(_PII_PATTERNS)
        if countries:
            for country in countries:
                country_upper = country.upper()
                if country_upper in _COUNTRY_PATTERNS:
                    self._patterns.update(_COUNTRY_PATTERNS[country_upper])
        if custom_patterns:
            for name, (pattern, replacement) in custom_patterns.items():
                self._patterns[name] = (re.compile(pattern), replacement)

        # NER scanner (lazy init)
        self._ner_scanner: NerPiiScanner | None = None
        if mode in ("ner", "rules_then_llm"):
            self._init_ner(ner_model)

        # LLM scanner (lazy init)
        self._llm_scanner: LlmPiiScanner | None = None
        if mode in ("llm", "rules_then_llm") and llm_provider:
            from astrocyte.policy.llm_scanner import LlmPiiScanner as _LlmScanner

            self._llm_scanner = _LlmScanner(llm_provider)

    def _init_ner(self, model: str) -> None:
        """Initialize NER scanner. Fails gracefully if spaCy not installed."""
        try:
            from astrocyte.policy.ner_scanner import NerPiiScanner as _NerScanner

            self._ner_scanner = _NerScanner(model)
        except ImportError:
            logger.warning("NER mode requested but spaCy not installed. Install with: pip install astrocyte[ner]")

    # ── Sync scanning (regex + NER) ──

    def scan(self, text: str) -> list[PiiMatch]:
        """Scan text for PII. Returns list of matches.

        Works for regex, ner, and rules_then_llm (regex+NER portion) modes.
        For llm mode, use scan_async().
        """
        if self.mode == "disabled":
            return []

        matches = self._scan_regex(text)

        # Include NER for ner mode and rules_then_llm (NER is part of "rules")
        if self.mode in ("ner", "rules_then_llm") and self._ner_scanner:
            ner_matches = self._ner_scanner.scan(text)
            matches = self._merge_matches(matches, ner_matches)

        return matches

    def _scan_regex(self, text: str) -> list[PiiMatch]:
        """Regex-only scan. Sync, pure."""
        matches: list[PiiMatch] = []
        for pii_type, (pattern, replacement) in self._patterns.items():
            for m in pattern.finditer(text):
                matched_text = m.group()

                # Credit card: validate with Luhn
                if pii_type == "credit_card" and not _luhn_check(matched_text):
                    continue

                # CA SIN: validate with Luhn
                if pii_type == "ca_sin" and not _luhn_check(matched_text):
                    continue

                matches.append(
                    PiiMatch(
                        pii_type=pii_type,
                        start=m.start(),
                        end=m.end(),
                        matched_text=matched_text,
                        replacement=replacement,
                    )
                )

        return matches

    @staticmethod
    def _merge_matches(a: list[PiiMatch], b: list[PiiMatch]) -> list[PiiMatch]:
        """Merge two match lists, removing overlaps (prefer earlier/longer)."""
        combined = sorted(a + b, key=lambda m: (m.start, -(m.end - m.start)))
        merged: list[PiiMatch] = []
        last_end = -1
        for match in combined:
            if match.start >= last_end:
                merged.append(match)
                last_end = match.end
        return merged

    # ── Async scanning (LLM + rules_then_llm) ──

    async def scan_async(self, text: str) -> list[PiiMatch]:
        """Async scan — supports all modes including LLM."""
        if self.mode in ("disabled", "regex", "ner"):
            return self.scan(text)

        if self.mode == "llm" and self._llm_scanner:
            return await self._llm_scanner.scan(text)

        if self.mode == "rules_then_llm":
            # Try regex + NER first
            matches = self.scan(text)
            if matches:
                return matches
            # Fall back to LLM
            if self._llm_scanner:
                return await self._llm_scanner.scan(text)

        return self.scan(text)

    # ── Apply actions ──

    def apply(self, text: str) -> tuple[str, list[PiiMatch]]:
        """Scan and apply action. Returns (processed_text, matches).

        Raises PiiRejected if action is 'reject' and PII is found.
        """
        matches = self.scan(text)
        return self._apply_matches(text, matches)

    async def apply_async(self, text: str) -> tuple[str, list[PiiMatch]]:
        """Async scan and apply — supports LLM modes."""
        matches = await self.scan_async(text)
        return self._apply_matches(text, matches)

    def _apply_matches(self, text: str, matches: list[PiiMatch]) -> tuple[str, list[PiiMatch]]:
        """Apply action to detected matches."""
        if not matches:
            return text, []

        # Check per-type overrides for reject
        reject_types = []
        for match in matches:
            override = self._type_overrides.get(match.pii_type)
            if override and override.get("action") == "reject":
                reject_types.append(match.pii_type)
            elif not override and self.action == "reject":
                reject_types.append(match.pii_type)

        if reject_types:
            raise PiiRejected(reject_types)

        # Apply per-type actions
        if self.action == "redact" or any(
            self._type_overrides.get(m.pii_type, {}).get("action") == "redact" for m in matches
        ):
            result = text
            for match in sorted(matches, key=lambda m: m.start, reverse=True):
                override = self._type_overrides.get(match.pii_type)
                action = override.get("action", self.action) if override else self.action
                if action == "redact":
                    replacement = (
                        override.get("replacement", match.replacement)
                        if override
                        else match.replacement
                    )
                    result = result[: match.start] + (replacement or "[REDACTED]") + result[match.end :]
                # warn: leave in place
            return result, matches

        # action == "warn": return original text with matches for logging
        return text, matches


# ---------------------------------------------------------------------------
# Content validator
# ---------------------------------------------------------------------------


class ContentValidator:
    """Validates retain content against policy rules.

    Sync, stateless — Rust migration candidate.
    """

    def __init__(
        self,
        max_content_length: int = 50000,
        reject_empty: bool = True,
        allowed_content_types: list[str] | None = None,
    ) -> None:
        self.max_content_length = max_content_length
        self.reject_empty = reject_empty
        self.allowed_content_types = allowed_content_types or ["text", "conversation", "document"]

    def validate(self, content: str, content_type: str = "text") -> list[str]:
        """Validate content. Returns list of error messages (empty = valid)."""
        errors: list[str] = []

        if self.reject_empty and not content.strip():
            errors.append("Content is empty")

        if len(content) > self.max_content_length:
            errors.append(f"Content too long: {len(content)} > {self.max_content_length}")

        if content_type not in self.allowed_content_types:
            errors.append(f"Content type '{content_type}' not allowed. Allowed: {self.allowed_content_types}")

        return errors


# ---------------------------------------------------------------------------
# Metadata sanitizer
# ---------------------------------------------------------------------------


class MetadataSanitizer:
    """Strips blocked keys and enforces size limits on metadata.

    Sync, stateless — Rust migration candidate.
    """

    def __init__(
        self,
        blocked_keys: list[str] | None = None,
        max_size_bytes: int = 4096,
    ) -> None:
        self.blocked_keys = set(blocked_keys or ["api_key", "password", "token", "secret"])
        self.max_size_bytes = max_size_bytes

    def sanitize(self, metadata: Metadata | None) -> tuple[Metadata | None, list[str]]:
        """Sanitize metadata. Returns (cleaned_metadata, warnings)."""
        if metadata is None:
            return None, []

        warnings: list[str] = []
        cleaned: Metadata = {}

        for key, value in metadata.items():
            if key.lower() in self.blocked_keys or any(bk in key.lower() for bk in self.blocked_keys):
                warnings.append(f"Blocked metadata key: '{key}'")
                continue
            cleaned[key] = value

        # Size check — remove keys in reverse alphabetical order (deterministic)
        serialized = json.dumps(cleaned, default=str)
        if len(serialized.encode("utf-8")) > self.max_size_bytes:
            warnings.append(f"Metadata exceeds {self.max_size_bytes} bytes, truncated")
            keys_by_priority = sorted(cleaned.keys(), reverse=True)  # z→a: drop least likely important first
            for drop_key in keys_by_priority:
                if len(json.dumps(cleaned, default=str).encode("utf-8")) <= self.max_size_bytes:
                    break
                cleaned.pop(drop_key)

        return cleaned if cleaned else None, warnings
