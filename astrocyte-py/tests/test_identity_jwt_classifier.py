"""Tests for :mod:`astrocyte.identity_jwt` — pure classification.

These tests exercise the JWT-claim-to-ActorIdentity mapping only. They do
NOT cover token signature verification, JWKS retrieval, expiry, audience,
or issuer validation — those belong to the MCP wiring layer (see
``docs/_plugins/jwt-identity-middleware.md``).

The test table mirrors the "Test cases" table in the identity spec
(§3 Gap 1) so there is a direct one-to-one mapping between the spec and
the suite.
"""

from __future__ import annotations

import pytest

from astrocyte.errors import AuthorizationError
from astrocyte.identity_jwt import classify_jwt_claims, derive_bank_id

# ---------------------------------------------------------------------------
# Delegated user tokens
# ---------------------------------------------------------------------------


class TestDelegatedUserTokens:
    """Claim patterns produced by OAuth 2.0 authorization code / OBO flows.

    Every test here must pin:
    1. ``type == "user"`` — routing decisions depend on this discriminator.
    2. ``id == <immutable subject>`` — NEVER email/UPN, even when both
       are present.
    3. ``claims["upn"]`` carries the display identifier for audit.
    """

    def test_entra_style_upn_and_oid(self) -> None:
        """Entra ID (AAD) v2 delegated user token: ``upn`` + ``oid``."""
        claims = {
            "upn": "alice@example.com",
            "oid": "00000000-0000-0000-0000-000000000001",
            "sub": "should-not-win-oid-does",
            "tid": "tenant-abc",
            "aud": "api://astrocyte",
        }
        identity = classify_jwt_claims(claims)

        assert identity.type == "user"
        # OID wins over sub because oid comes first in _USER_SUBJECT_CLAIMS.
        # This is correct for Entra — sub is per-app-pair, oid is per-user.
        assert identity.id == "00000000-0000-0000-0000-000000000001"
        assert identity.claims == {
            "upn": "alice@example.com",
            "tenant_id": "tenant-abc",
        }

    def test_standard_oidc_preferred_username_and_sub(self) -> None:
        """Generic OIDC delegated user token: ``preferred_username`` + ``sub``."""
        claims = {
            "preferred_username": "bob@corp",
            "sub": "user-12345",
        }
        identity = classify_jwt_claims(claims)

        assert identity.type == "user"
        assert identity.id == "user-12345"
        assert identity.claims == {"upn": "bob@corp"}

    def test_email_claim_alone_is_sufficient_display(self) -> None:
        """Some IdPs only emit ``email``. Still a valid user token."""
        claims = {
            "email": "carol@startup.io",
            "sub": "abc-789",
        }
        identity = classify_jwt_claims(claims)

        assert identity.type == "user"
        assert identity.id == "abc-789"
        assert identity.claims == {"upn": "carol@startup.io"}

    def test_user_display_without_subject_is_rejected(self) -> None:
        """Display claim but no stable subject → fail closed.

        The spec requires a stable bank key. An email-only token would
        leak cross-user memory the moment the email changes.
        """
        claims = {"email": "d@e.com"}
        with pytest.raises(AuthorizationError, match="no stable subject"):
            classify_jwt_claims(claims)

    def test_idtyp_app_overrides_user_display_claims(self) -> None:
        """A token with ``idtyp=app`` is a service account even if it
        also carries user-looking claims. Entra ID v2 uses this signal
        specifically because service accounts can legitimately have a
        ``sub`` that looks like a user identifier."""
        claims = {
            "idtyp": "app",
            "upn": "noreply@example.com",
            "sub": "should-not-win",
            "appid": "app-abc",
            "tid": "tenant-xyz",
        }
        identity = classify_jwt_claims(claims)

        assert identity.type == "service"
        assert identity.id == "app-abc"
        # upn is NOT stashed because this is a service account.
        assert identity.claims is not None
        assert identity.claims.get("upn") is None
        assert identity.claims.get("app_id") == "app-abc"
        assert identity.claims.get("idtyp") == "app"


# ---------------------------------------------------------------------------
# Service account tokens
# ---------------------------------------------------------------------------


class TestServiceAccountTokens:
    """Claim patterns produced by the OAuth 2.0 client credentials flow."""

    def test_entra_style_appid(self) -> None:
        claims = {
            "appid": "app-abc-123",
            "tid": "tenant-xyz",
        }
        identity = classify_jwt_claims(claims)

        assert identity.type == "service"
        assert identity.id == "app-abc-123"
        assert identity.claims == {"app_id": "app-abc-123", "tenant_id": "tenant-xyz"}

    def test_oidc_azp_without_user_claims(self) -> None:
        """OIDC ``azp`` (authorized party) + no user display → service."""
        claims = {"azp": "app-azp-999"}
        identity = classify_jwt_claims(claims)

        assert identity.type == "service"
        assert identity.id == "app-azp-999"

    def test_generic_client_id(self) -> None:
        """Some IdPs only emit ``client_id``."""
        claims = {"client_id": "client-42"}
        identity = classify_jwt_claims(claims)

        assert identity.type == "service"
        assert identity.id == "client-42"

    def test_appid_wins_over_azp_when_both_present(self) -> None:
        """First match in :data:`_SERVICE_APP_CLAIMS` wins. ``appid``
        takes precedence over ``azp`` / ``client_id``. The ordering is
        fixed so deployments can reason about it deterministically."""
        claims = {
            "appid": "primary-app",
            "azp": "secondary-app",
            "client_id": "tertiary-app",
        }
        identity = classify_jwt_claims(claims)
        assert identity.id == "primary-app"

    def test_idtyp_app_without_appid_fails_closed(self) -> None:
        """``idtyp=app`` asserts service account but provides no app id —
        refuse rather than invent one."""
        claims = {"idtyp": "app"}
        with pytest.raises(AuthorizationError, match="no app id claim"):
            classify_jwt_claims(claims)


# ---------------------------------------------------------------------------
# Failure modes — fail closed, always
# ---------------------------------------------------------------------------


class TestFailClosed:
    """Every unexpected claim shape must raise, never silently degrade.

    The failure mode being prevented is cross-user memory leakage: a
    partially-valid token falling through to a shared default bank.
    """

    def test_empty_claims(self) -> None:
        with pytest.raises(AuthorizationError, match="identity type could not be determined"):
            classify_jwt_claims({})

    def test_claims_not_a_dict(self) -> None:
        with pytest.raises(AuthorizationError, match="must be a dict"):
            classify_jwt_claims("not a claim dict")  # type: ignore[arg-type]

    def test_whitespace_only_claim_values_ignored(self) -> None:
        """Blank strings should not count as 'claim present'."""
        claims = {"upn": "   ", "sub": "  "}
        with pytest.raises(AuthorizationError, match="identity type could not be determined"):
            classify_jwt_claims(claims)

    def test_non_string_claim_values_ignored(self) -> None:
        """Non-string values should not be accepted as identifiers."""
        claims = {"appid": 12345, "upn": None}
        with pytest.raises(AuthorizationError, match="identity type could not be determined"):
            classify_jwt_claims(claims)

    def test_error_message_includes_available_claim_keys(self) -> None:
        """Operators diagnosing a bad deployment need to see which claim
        names the IdP actually sent so they can match them against the
        expected sets."""
        claims = {"weird": "value", "another": "thing"}
        with pytest.raises(AuthorizationError) as excinfo:
            classify_jwt_claims(claims)
        msg = str(excinfo.value)
        assert "weird" in msg
        assert "another" in msg


# ---------------------------------------------------------------------------
# Bank id derivation (policy layer, separate from classification)
# ---------------------------------------------------------------------------


class TestDeriveBankId:
    def test_user_default_prefix(self) -> None:
        claims = {"sub": "user-1", "email": "x@y.com"}
        identity = classify_jwt_claims(claims)
        assert derive_bank_id(identity) == "user-user-1"  # prefix + id

    def test_service_default_prefix(self) -> None:
        claims = {"appid": "svc-a"}
        identity = classify_jwt_claims(claims)
        assert derive_bank_id(identity) == "service-svc-a"

    def test_service_with_spec_convention_svc_prefix(self) -> None:
        """The spec prefers ``svc-`` over ``service-``. Prefix is a
        deployment choice, not a core convention."""
        claims = {"appid": "app-42"}
        identity = classify_jwt_claims(claims)
        assert derive_bank_id(identity, service_bank_prefix="svc-") == "svc-app-42"

    def test_unsupported_identity_type_raises(self) -> None:
        """The classifier only emits user/service, but derive_bank_id
        must guard against agent identities slipping through (those
        come from the legacy BankResolver, not JWT)."""
        from astrocyte.types import ActorIdentity

        agent = ActorIdentity(type="agent", id="a1")
        with pytest.raises(ValueError, match="only 'user' and 'service'"):
            derive_bank_id(agent)


# ---------------------------------------------------------------------------
# Regression pins — behaviors load-bearing for compliance
# ---------------------------------------------------------------------------


class TestComplianceRegressionPins:
    """Behaviors the spec makes compliance claims about. These tests must
    fail loudly if the classifier is ever changed in a way that breaks
    PDPA / GDPR / MAS TRM attribution."""

    def test_email_never_becomes_bank_key_when_sub_exists(self) -> None:
        """PDPA / GDPR attribution requires a stable bank key. Email is
        mutable; subject identifiers are not. If a regression routes by
        email, per-user erasure stops working."""
        claims = {
            "email": "alice-before@example.com",
            "sub": "stable-subject-id",
        }
        identity = classify_jwt_claims(claims)
        assert identity.id == "stable-subject-id"
        assert identity.id != "alice-before@example.com"

    def test_service_identity_does_not_leak_user_display(self) -> None:
        """A token with idtyp=app must not stash a user display
        identifier — audit logs must not claim a service account is a
        specific human."""
        claims = {"idtyp": "app", "upn": "service-nicky@example.com", "appid": "app-x"}
        identity = classify_jwt_claims(claims)
        assert identity.claims is not None
        assert "upn" not in identity.claims
