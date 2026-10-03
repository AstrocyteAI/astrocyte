"""Credential redaction in the retain barrier and the capture spool.

Memory is replayed into later prompts, possibly for another agent and model
vendor than the one it came from, so a pasted key must not survive retain.
Fixtures are fake but format-valid; built by concatenation so this file
itself never matches a secret scanner.
"""

from __future__ import annotations

import pytest

from astrocyte.policy.barriers import PiiScanner, redact_secrets

R = "[SECRET_REDACTED]"

SECRETS = {
    "openai": "sk-" + "proj-" + "Ab3" * 15,
    "anthropic": "sk-" + "ant-api03-" + "Zx9_" * 12,
    "aws": "AKIA" + "IOSFODNN7EXAMPLE",
    "github": "ghp" + "_" + "a1B2" * 9,
    "github_pat": "github_pat" + "_" + "11ABCDEFG0" * 4,
    "gitlab": "glpat" + "-" + "x1Y2z3" * 4,
    "slack": "xox" + "b-" + "1234567890-" + "abcdefghij",
    "stripe": "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "google": "AIza" + "SyA1" + "b" * 31,
    "huggingface": "hf" + "_" + "aB3" * 11,
    "npm": "npm" + "_" + "a1" * 18,
    "doppler": "dp" + ".st." + "prd." + "A1b2C3d4" * 4,
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
}


@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_known_credential_formats_are_redacted(kind):
    secret = SECRETS[kind]
    text = f"use this one: {secret} and tell me if it works"
    assert redact_secrets(text) == f"use this one: {R} and tell me if it works"
    redacted, matches = PiiScanner().apply(text)
    assert secret not in redacted and R in redacted, matches


def test_private_key_blocks_are_redacted_whole_even_if_truncated():
    begin, end = "-----BEGIN " + "OPENSSH PRIVATE KEY-----", "-----END " + "OPENSSH PRIVATE KEY-----"
    block = f"{begin}\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\nAAAAAAABAAAAMwAAAAtzc2gtZW\n{end}"
    assert redact_secrets(f"key:\n{block}\nthanks") == f"key:\n{R}\nthanks"
    # A paste cut off before the END line still loses everything after BEGIN.
    assert redact_secrets(f"key:\n{begin}\nb3BlbnNzaC1rZXktdjEAAAA") == f"key:\n{R}"


@pytest.mark.parametrize("text,expected", [
    ("DB_PASSWORD=hunter2hunter2", f"DB_PASSWORD={R}"),
    ('export OPENAI_API_KEY="abc123def456ghi789"', f'export OPENAI_API_KEY="{R}"'),
    ('{"client_secret": "9f8e7d6c5b4a3f2e1d"}', f'{{"client_secret": "{R}"}}'),
    ("postgres://app:s3cretPass@db.internal:5432/app", f"postgres://app:{R}@db.internal:5432/app"),
])
def test_values_are_redacted_and_names_kept(text, expected):
    """The name is useful memory ("the app reads DB_PASSWORD"); the value isn't."""
    assert redact_secrets(text) == expected
    # The full barrier may redact more (the email pattern also claims
    # "pass@host" in a URL), never less.
    secret = text[expected.index(R):len(text) - (len(expected) - expected.index(R) - len(R))]
    assert secret not in PiiScanner().apply(text)[0]


@pytest.mark.parametrize("text", [
    "commit 3f2c1a9b8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b fixed it",  # git SHA
    "request id 123e4567-e89b-12d3-a456-426614174000",  # UUID
    "export OPENAI_API_KEY=$OPENAI_API_KEY",  # a reference, not a value
    "api_key=os.environ['OPENAI_API_KEY']",
    "set max_tokens=1024 and token_budget: 200000000000",  # counts, no letters
    "password: <your password here>",
    "the token is stored in 1Password under 'staging'",
    "we use sk-learn-compatible-estimators-everywhere",  # no digit: an identifier
    "see https://github.com/org/repo/pull/123",
    "authenticate with an API key from the dashboard",
])
def test_ordinary_engineering_text_is_left_alone(text):
    assert redact_secrets(text) == text


def test_overlapping_matches_do_not_corrupt_text():
    """A phone-shaped digit run inside a key overlapped the key's span;
    redacting both back to front spliced with stale offsets."""
    key = "sk-" + "a" * 6 + "5551234567" + "b" * 10
    text = f"key {key} end"
    redacted, _ = PiiScanner().apply(text)
    assert redacted == f"key {R} end"


def test_disabled_mode_redacts_nothing():
    text = "DB_PASSWORD=hunter2hunter2"
    assert PiiScanner(mode="disabled").apply(text)[0] == text


def test_type_overrides_apply_to_credentials():
    """Credentials are ordinary barrier types: an operator can turn one off."""
    scanner = PiiScanner(type_overrides={"credential_assignment": {"action": "warn"}})
    text = "DB_PASSWORD=hunter2hunter2"
    out, matches = scanner.apply(text)
    assert out == text and [m.pii_type for m in matches] == ["credential_assignment"]
