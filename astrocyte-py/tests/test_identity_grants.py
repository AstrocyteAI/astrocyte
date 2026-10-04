"""Glob grants, ``team:`` groups and context-bound grants (team memory G1)."""

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import AccessDenied
from astrocyte.identity import accessible_read_banks, effective_permissions, format_principal, parse_principal
from astrocyte.testing.in_memory import InMemoryEngineProvider
from astrocyte.types import AccessGrant, ActorIdentity, AstrocyteContext


def _grant(bank_id: str, principal: str, *perms: str) -> AccessGrant:
    return AccessGrant(bank_id=bank_id, principal=principal, permissions=list(perms))


def _perms(principal: str, configured: list[AccessGrant], bank_id: str, /, **ctx_kw) -> set[str]:
    return effective_permissions(AstrocyteContext(principal=principal, **ctx_kw), configured, bank_id)


class TestBankGlobs:
    grants = [_grant("project:*", "user:alice", "read", "write")]

    @pytest.mark.parametrize("bank_id", ["project:api-1a2b3c", "project:x", "project:a:b"])
    def test_prefix_glob_matches(self, bank_id):
        assert _perms("user:alice", self.grants, bank_id) == {"read", "write"}

    @pytest.mark.parametrize("bank_id", ["projectx", "project", "myproject:x", "Project:x"])
    def test_prefix_glob_does_not_match(self, bank_id):
        # The colon is literal, and matching is case-sensitive on every platform.
        assert _perms("user:alice", self.grants, bank_id) == set()

    def test_question_mark_and_class(self):
        grants = [_grant("shard-?", "user:alice", "read"), _grant("env-[ab]", "user:alice", "write")]
        assert _perms("user:alice", grants, "shard-1") == {"read"}
        assert _perms("user:alice", grants, "shard-10") == set()
        assert _perms("user:alice", grants, "env-a") == {"write"}
        assert _perms("user:alice", grants, "env-c") == set()

    def test_exact_and_star_unchanged(self):
        grants = [_grant("b1", "user:alice", "read"), _grant("*", "user:alice", "forget")]
        assert _perms("user:alice", grants, "b1") == {"read", "forget"}
        assert _perms("user:alice", grants, "b2") == {"forget"}

    def test_principal_glob(self):
        grants = [_grant("shared", "user:*", "read")]
        assert _perms("user:bob", grants, "shared") == {"read"}
        assert _perms("agent:bob", grants, "shared") == set()


class TestTeamGroups:
    def test_team_principal_parses_as_team(self):
        actor = parse_principal("team:api")
        assert actor == ActorIdentity(type="team", id="api")
        assert format_principal(actor) == "team:api"

    def test_group_grant_applies_to_member(self):
        grants = [_grant("project:*", "team:api", "read")]
        assert _perms("user:alice", grants, "project:x", groups=["team:api"]) == {"read"}

    def test_group_grant_does_not_apply_to_non_member(self):
        grants = [_grant("project:*", "team:api", "read")]
        assert _perms("user:alice", grants, "project:x") == set()
        assert _perms("user:alice", grants, "project:x", groups=["team:web"]) == set()

    def test_groups_union_with_own_grants(self):
        grants = [_grant("b1", "team:api", "read"), _grant("b1", "user:alice", "write")]
        assert _perms("user:alice", grants, "b1", groups=["team:api"]) == {"read", "write"}

    def test_groups_do_not_extend_on_behalf_of(self):
        grants = [_grant("b1", "agent:bot", "read", "write"), _grant("b1", "team:api", "read", "write")]
        ctx = AstrocyteContext(
            principal="",
            actor=ActorIdentity(type="agent", id="bot"),
            on_behalf_of=ActorIdentity(type="user", id="calvin"),
            groups=["team:api"],
        )
        assert effective_permissions(ctx, grants, "b1") == set()


class TestContextGrants:
    def test_context_grants_add_to_configured(self):
        grants = [_grant("b1", "user:alice", "read")]
        ctx_grants = [_grant("b1", "user:alice", "write")]
        assert _perms("user:alice", grants, "b1", grants=ctx_grants) == {"read", "write"}

    def test_context_grants_still_match_on_principal(self):
        ctx_grants = [_grant("b1", "user:bob", "write")]
        assert _perms("user:alice", [], "b1", grants=ctx_grants) == set()

    def test_accessible_read_banks_uses_context_grants_and_skips_patterns(self):
        ctx = AstrocyteContext(
            principal="user:alice",
            grants=[_grant("project:api", "user:alice", "read"), _grant("project:*", "user:alice", "read")],
        )
        grants = [_grant("team-*", "user:alice", "read")]
        assert accessible_read_banks(ctx, grants, known_bank_ids=["project:web"]) == ["project:api", "project:web"]


class TestEnforcedThroughBrain:
    def _brain(self, grants: list[AccessGrant]) -> Astrocyte:
        config = AstrocyteConfig()
        config.provider = "test"
        config.access_control.enabled = True
        config.access_control.default_policy = "deny"
        brain = Astrocyte(config)
        brain.set_engine_provider(InMemoryEngineProvider())
        brain.set_access_grants(grants)
        return brain

    async def test_glob_and_group_grants_enforced(self):
        brain = self._brain([_grant("project:*", "team:api", "read", "write")])
        member = AstrocyteContext(principal="user:alice", groups=["team:api"])
        assert (await brain.retain("x", bank_id="project:api", context=member)).stored is True
        with pytest.raises(AccessDenied):
            await brain.retain("x", bank_id="projectx", context=member)
        with pytest.raises(AccessDenied):
            await brain.retain("x", bank_id="project:api", context=AstrocyteContext(principal="user:bob"))

    async def test_read_only_context_grant_refuses_retain(self):
        brain = self._brain([])
        ctx = AstrocyteContext(principal="user:alice", grants=[_grant("project:*", "user:alice", "read")])
        with pytest.raises(AccessDenied):
            await brain.retain("x", bank_id="project:api", context=ctx)
        await brain.recall("x", bank_id="project:api", context=ctx)
