"""M1 (v0.5.0) identity: ActorIdentity, OBO intersection, BankResolver, auto-resolve."""

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import AccessDenied, ConfigError
from astrocyte.identity import BankResolver, accessible_read_banks, effective_permissions, resolve_actor
from astrocyte.testing.in_memory import InMemoryEngineProvider
from astrocyte.types import AccessGrant, ActorIdentity, AstrocyteContext


def _brain_acl(**identity_kw) -> Astrocyte:
    config = AstrocyteConfig()
    config.provider = "test"
    config.access_control.enabled = True
    config.access_control.default_policy = "deny"
    for k, v in identity_kw.items():
        setattr(config.identity, k, v)
    brain = Astrocyte(config)
    brain.set_engine_provider(InMemoryEngineProvider())
    return brain


class TestEffectivePermissions:
    def test_obo_intersection_denies_write(self):
        grants = [
            AccessGrant(bank_id="b1", principal="agent:bot", permissions=["read", "write"]),
            AccessGrant(bank_id="b1", principal="user:calvin", permissions=["read"]),
        ]
        ctx = AstrocyteContext(
            principal="ignored",
            actor=ActorIdentity(type="agent", id="bot"),
            on_behalf_of=ActorIdentity(type="user", id="calvin"),
        )
        assert effective_permissions(ctx, grants, "b1") == {"read"}

    def test_obo_intersection_allows_when_both_write(self):
        grants = [
            AccessGrant(bank_id="b1", principal="agent:bot", permissions=["read", "write"]),
            AccessGrant(bank_id="b1", principal="user:calvin", permissions=["read", "write"]),
        ]
        ctx = AstrocyteContext(
            principal="",
            actor=ActorIdentity(type="agent", id="bot"),
            on_behalf_of=ActorIdentity(type="user", id="calvin"),
        )
        assert effective_permissions(ctx, grants, "b1") == {"read", "write"}

    def test_actor_overrides_principal_string(self):
        grants = [
            AccessGrant(bank_id="b1", principal="agent:good", permissions=["write"]),
        ]
        ctx = AstrocyteContext(principal="agent:evil", actor=ActorIdentity(type="agent", id="good"))
        assert "write" in effective_permissions(ctx, grants, "b1")


class TestResolveActor:
    def test_explicit_actor(self):
        ctx = AstrocyteContext(principal="user:nope", actor=ActorIdentity(type="agent", id="a1"))
        assert resolve_actor(ctx) == ActorIdentity(type="agent", id="a1")

    def test_parse_principal(self):
        ctx = AstrocyteContext(principal="user:calvin")
        a = resolve_actor(ctx)
        assert a is not None and a.type == "user" and a.id == "calvin"


class TestBankResolver:
    def test_default_bank_ids(self):
        r = BankResolver()
        assert r.default_bank_id(ActorIdentity(type="user", id="calvin")) == "user-calvin"
        assert r.default_bank_id(ActorIdentity(type="agent", id="bot")) == "agent-bot"


class TestAccessibleReadBanks:
    def test_wildcard_bank_with_resolver(self):
        grants = [
            AccessGrant(bank_id="*", principal="user:alice", permissions=["read"]),
        ]
        ctx = AstrocyteContext(principal="user:alice")
        r = BankResolver()
        banks = accessible_read_banks(ctx, grants, resolver=r)
        assert banks == ["user-alice"]


class TestAstrocyteObo:
    async def test_retain_denied_on_obo_intersection(self):
        brain = _brain_acl()
        brain.set_access_grants(
            [
                AccessGrant(bank_id="bank-1", principal="agent:bot", permissions=["read", "write"]),
                AccessGrant(bank_id="bank-1", principal="user:calvin", permissions=["read"]),
            ]
        )
        ctx = AstrocyteContext(
            principal="",
            actor=ActorIdentity(type="agent", id="bot"),
            on_behalf_of=ActorIdentity(type="user", id="calvin"),
        )
        with pytest.raises(AccessDenied):
            await brain.retain("x", bank_id="bank-1", context=ctx)

    async def test_retain_allowed_when_obo_allows_write(self):
        brain = _brain_acl()
        brain.set_access_grants(
            [
                AccessGrant(bank_id="bank-1", principal="agent:bot", permissions=["read", "write"]),
                AccessGrant(bank_id="bank-1", principal="user:calvin", permissions=["read", "write"]),
            ]
        )
        ctx = AstrocyteContext(
            principal="",
            actor=ActorIdentity(type="agent", id="bot"),
            on_behalf_of=ActorIdentity(type="user", id="calvin"),
        )
        result = await brain.retain("ok", bank_id="bank-1", context=ctx)
        assert result.stored is True


class TestAutoResolveBanks:
    async def test_recall_without_bank_id_resolves_from_grants(self):
        brain = _brain_acl(auto_resolve_banks=True)
        brain.set_access_grants(
            [
                AccessGrant(bank_id="user-alice", principal="user:alice", permissions=["read", "write"]),
            ]
        )
        ctx = AstrocyteContext(principal="user:alice")
        await brain.retain("pinned fact", bank_id="user-alice", context=ctx)
        out = await brain.recall("pinned", context=ctx)
        assert len(out.hits) >= 1

    async def test_recall_without_bank_raises_when_auto_resolve_off(self):
        brain = _brain_acl(auto_resolve_banks=False)
        with pytest.raises(ConfigError, match="Either bank_id or banks"):
            await brain.recall("q", context=AstrocyteContext(principal="user:x"))
