"""Barrier policies — PII scanning, content validation, metadata sanitization.

All functions are sync (Rust migration candidates).
See docs/_design/policy-layer.md section 2 and docs/_design/data-governance.md.
"""

from __future__ import annotations

import json
import re

from astrocytes.errors import PiiRejected
from astrocytes.types import Metadata, PiiMatch

# ---------------------------------------------------------------------------
# PII regex patterns
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
}


def _luhn_check(number: str) -> bool:
    """Validate credit card number with Luhn algorithm."""
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
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
    """Regex-based PII detection.

    Sync, stateless — Rust migration candidate.
    """

    def __init__(
        self,
        mode: str = "regex",
        action: str = "redact",
        custom_patterns: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.mode = mode
        self.action = action  # "redact" | "reject" | "warn"
        self._patterns = dict(_PII_PATTERNS)
        if custom_patterns:
            for name, (pattern, replacement) in custom_patterns.items():
                self._patterns[name] = (re.compile(pattern), replacement)

    def scan(self, text: str) -> list[PiiMatch]:
        """Scan text for PII. Returns list of matches."""
        if self.mode == "disabled":
            return []

        matches: list[PiiMatch] = []
        for pii_type, (pattern, replacement) in self._patterns.items():
            for m in pattern.finditer(text):
                matched_text = m.group()

                # Credit card: validate with Luhn
                if pii_type == "credit_card" and not _luhn_check(matched_text):
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

    def apply(self, text: str) -> tuple[str, list[PiiMatch]]:
        """Scan and apply action. Returns (processed_text, matches).

        Raises PiiRejected if action is 'reject' and PII is found.
        """
        matches = self.scan(text)
        if not matches:
            return text, []

        if self.action == "reject":
            raise PiiRejected([m.pii_type for m in matches])

        if self.action == "redact":
            # Apply replacements from right to left to preserve offsets
            result = text
            for match in sorted(matches, key=lambda m: m.start, reverse=True):
                result = result[: match.start] + (match.replacement or "[REDACTED]") + result[match.end :]
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

        # Size check
        serialized = json.dumps(cleaned, default=str)
        if len(serialized.encode("utf-8")) > self.max_size_bytes:
            warnings.append(f"Metadata exceeds {self.max_size_bytes} bytes, truncated")
            # Truncate by removing keys until under limit
            while len(json.dumps(cleaned, default=str).encode("utf-8")) > self.max_size_bytes and cleaned:
                cleaned.pop(next(iter(cleaned)))

        return cleaned if cleaned else None, warnings
