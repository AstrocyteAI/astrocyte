"""M4 — HMAC verification for webhook ingest (TDD)."""

from __future__ import annotations

import hashlib
import hmac

from astrocyte.ingest.hmac_auth import compute_hmac_sha256_hex, normalize_signature_header, verify_hmac_sha256


class TestHmacSha256:
    def test_compute_matches_hmac_digest(self):
        body = b'{"content":"hello"}'
        secret = "s3cr3t"
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        assert compute_hmac_sha256_hex(secret, body) == expected

    def test_verify_accepts_matching_hex(self):
        body = b"payload"
        secret = "key"
        sig = compute_hmac_sha256_hex(secret, body)
        assert verify_hmac_sha256(secret, body, sig) is True

    def test_verify_rejects_tampered_body(self):
        body = b"payload"
        secret = "key"
        sig = compute_hmac_sha256_hex(secret, body)
        assert verify_hmac_sha256(secret, body + b"x", sig) is False

    def test_normalize_github_style_prefix(self):
        body = b"x"
        secret = "k"
        raw = compute_hmac_sha256_hex(secret, body)
        assert normalize_signature_header(f"sha256={raw}") == raw
        assert verify_hmac_sha256(secret, body, f"sha256={raw}") is True
