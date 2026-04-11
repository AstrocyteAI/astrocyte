"""HMAC signature helpers for webhook ingest (M4)."""

from __future__ import annotations

import hashlib
import hmac


def compute_hmac_sha256_hex(secret: str, body: bytes) -> str:
    """Return lowercase hex digest of HMAC-SHA256(secret, body)."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def normalize_signature_header(value: str) -> str:
    """Strip ``sha256=`` / ``sha1=`` prefix if present (GitHub-style)."""
    v = value.strip()
    for prefix in ("sha256=", "sha1="):
        if v.lower().startswith(prefix):
            return v.split("=", 1)[1].strip()
    return v


def verify_hmac_sha256(secret: str, body: bytes, signature_header: str) -> bool:
    """Constant-time compare of computed HMAC to header value (hex)."""
    expected = compute_hmac_sha256_hex(secret, body)
    got = normalize_signature_header(signature_header)
    if len(got) != len(expected):
        return False
    return hmac.compare_digest(expected.lower(), got.lower())
