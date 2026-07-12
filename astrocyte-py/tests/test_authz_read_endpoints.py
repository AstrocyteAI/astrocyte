"""Authorization regression tests for the read/compute endpoints.

Before this fix, ``compile``, ``graph_search``, ``graph_neighbors``,
``history``, and ``audit`` did not thread the caller's ``context`` into an
access check — so with ``access_control`` enabled a principal granted read on
bank A could still read bank B (an intra-tenant IDOR). These tests pin the
invariant that every one of them enforces ``check_access`` before touching a
store or pipeline.

The check runs FIRST (before the ConfigError for a missing store/pipeline),
so an unauthorized caller gets ``AccessDenied`` even on an otherwise
unconfigured brain — which is exactly what we assert.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import AccessDenied
from astrocyte.types import AccessGrant, AstrocyteContext


def _brain_with_acl() -> Astrocyte:
    config = AstrocyteConfig()
    config.provider = "test"
    config.access_control.enabled = True
    config.access_control.default_policy = "deny"
    return Astrocyte(config)


def _grant_read(brain: Astrocyte, principal: str, bank: str) -> None:
    grants = list(brain._policy.access_grants)
    grants.append(AccessGrant(principal=principal, bank_id=bank, permissions=["read"]))
    brain._policy.set_access_grants(grants)


class TestReadEndpointAuthz:
    """Each endpoint must deny a principal lacking a grant on the target bank."""

    async def test_compile_denies_without_grant(self) -> None:
        brain = _brain_with_acl()
        ctx = AstrocyteContext(principal="user:alice")
        with pytest.raises(AccessDenied):
            await brain.compile("victim-bank", context=ctx)

    async def test_graph_search_denies_without_grant(self) -> None:
        brain = _brain_with_acl()
        ctx = AstrocyteContext(principal="user:alice")
        with pytest.raises(AccessDenied):
            await brain.graph_search("Alice", "victim-bank", context=ctx)

    async def test_graph_neighbors_denies_without_grant(self) -> None:
        brain = _brain_with_acl()
        ctx = AstrocyteContext(principal="user:alice")
        with pytest.raises(AccessDenied):
            await brain.graph_neighbors(["e1"], "victim-bank", context=ctx)

    async def test_history_denies_without_grant(self) -> None:
        brain = _brain_with_acl()
        ctx = AstrocyteContext(principal="user:alice")
        with pytest.raises(AccessDenied):
            await brain.history(
                "what did we know", "victim-bank", datetime.now(UTC), context=ctx
            )

    async def test_audit_denies_without_grant(self) -> None:
        brain = _brain_with_acl()
        ctx = AstrocyteContext(principal="user:alice")
        with pytest.raises(AccessDenied):
            await brain.audit("some topic", "victim-bank", context=ctx)

    async def test_cross_bank_grant_does_not_leak(self) -> None:
        """A read grant on bank A must NOT authorize graph_search on bank B."""
        brain = _brain_with_acl()
        _grant_read(brain, "user:alice", "bank-a")
        ctx = AstrocyteContext(principal="user:alice")
        # Granted bank passes the check (fails later on missing graph store —
        # a ConfigError, NOT AccessDenied — proving the check let it through).
        with pytest.raises(Exception) as granted:
            await brain.graph_search("q", "bank-a", context=ctx)
        assert not isinstance(granted.value, AccessDenied)
        # Ungranted sibling bank is denied at the access check.
        with pytest.raises(AccessDenied):
            await brain.graph_search("q", "bank-b", context=ctx)

    async def test_history_context_parameter_accepted(self) -> None:
        """Regression for the /v1/history 500: history() must accept context=."""
        brain = _brain_with_acl()
        _grant_read(brain, "user:alice", "bank-a")
        ctx = AstrocyteContext(principal="user:alice")
        # With the grant, the access check passes and we fail later (no
        # pipeline) — a non-AccessDenied error — rather than TypeError on the
        # keyword argument.
        with pytest.raises(Exception) as exc:
            await brain.history("q", "bank-a", datetime.now(UTC), context=ctx)
        assert not isinstance(exc.value, TypeError)
        assert not isinstance(exc.value, AccessDenied)
