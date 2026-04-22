"""Identity-aware MIP routing (identity spec §3 Gap 2).

Covers the match DSL extensions and action template interpolation that let
rules branch on caller identity:

- ``principal_type: user`` / ``principal_type: service``
- ``principal_id``, ``principal_upn``, ``principal_app_id``
- Dotted ``principal.type`` / ``principal.id`` / ``principal.oid_or_app_id``
  / ``principal.upn`` for action template interpolation
- Absent identity → principal_* conditions never fire (backward compat)

The tests go through the full :class:`MipRouter` path, not just
``resolve_field``, so we also pin the end-to-end routing behavior a
reference ``mip-enterprise-identity.yaml`` config depends on.
"""

from __future__ import annotations

from astrocyte.mip.router import MipRouter
from astrocyte.mip.rule_engine import RuleEngineInput, resolve_field
from astrocyte.mip.schema import (
    ActionSpec,
    MatchBlock,
    MatchSpec,
    MipConfig,
    RoutingRule,
)
from astrocyte.types import ActorIdentity

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _user_identity(oid: str = "user-oid-1", upn: str = "alice@example.com") -> ActorIdentity:
    return ActorIdentity(
        type="user",
        id=oid,
        claims={"upn": upn, "tenant_id": "t1"},
    )


def _service_identity(app_id: str = "app-abc") -> ActorIdentity:
    return ActorIdentity(
        type="service",
        id=app_id,
        claims={"app_id": app_id, "tenant_id": "t1"},
    )


def _rule(
    name: str,
    priority: int,
    *,
    bank: str,
    principal_type: str | None = None,
    principal_id: str | None = None,
    content_type: str | None = "text",
    tags: list[str] | None = None,
) -> RoutingRule:
    conds: list[MatchSpec] = []
    if content_type is not None:
        conds.append(MatchSpec(field="content_type", operator="eq", value=content_type))
    if principal_type is not None:
        conds.append(MatchSpec(field="principal_type", operator="eq", value=principal_type))
    if principal_id is not None:
        conds.append(MatchSpec(field="principal_id", operator="eq", value=principal_id))
    return RoutingRule(
        name=name,
        priority=priority,
        match=MatchBlock(all_conditions=conds),
        action=ActionSpec(bank=bank, tags=tags),
    )


def _input(**kw) -> RuleEngineInput:
    return RuleEngineInput(content="hi", content_type=kw.pop("content_type", "text"), **kw)


# ---------------------------------------------------------------------------
# resolve_field — pure lookups (the smallest contract the rule engine relies on)
# ---------------------------------------------------------------------------


class TestResolveIdentityFields:
    """Direct lookups against :func:`resolve_field`. These pin the wire
    between RuleEngineInput and the match/template evaluator; every
    higher-level test is downstream of these."""

    def test_principal_type_user(self) -> None:
        inp = _input(actor_identity=_user_identity())
        assert resolve_field("principal_type", inp) == "user"

    def test_principal_type_service(self) -> None:
        inp = _input(actor_identity=_service_identity())
        assert resolve_field("principal_type", inp) == "service"

    def test_principal_type_absent_identity_is_none(self) -> None:
        inp = _input()  # no actor_identity
        assert resolve_field("principal_type", inp) is None

    def test_principal_id_flat_form(self) -> None:
        inp = _input(actor_identity=_user_identity(oid="alice-oid"))
        assert resolve_field("principal_id", inp) == "alice-oid"

    def test_principal_upn_reads_from_claims(self) -> None:
        inp = _input(actor_identity=_user_identity(upn="alice@corp"))
        assert resolve_field("principal_upn", inp) == "alice@corp"

    def test_principal_app_id_reads_from_claims(self) -> None:
        inp = _input(actor_identity=_service_identity(app_id="svc-42"))
        assert resolve_field("principal_app_id", inp) == "svc-42"

    def test_principal_app_id_returns_none_for_user(self) -> None:
        """Users don't have an app_id claim — must return None, not crash."""
        inp = _input(actor_identity=_user_identity())
        assert resolve_field("principal_app_id", inp) is None

    def test_dotted_principal_type(self) -> None:
        inp = _input(actor_identity=_user_identity())
        assert resolve_field("principal.type", inp) == "user"

    def test_dotted_principal_id(self) -> None:
        inp = _input(actor_identity=_service_identity(app_id="svc-99"))
        assert resolve_field("principal.id", inp) == "svc-99"

    def test_dotted_oid_alias(self) -> None:
        """``principal.oid`` and ``principal.oid_or_app_id`` both alias
        to identity.id — this is the ergonomic form documented in the
        spec for bank template interpolation."""
        inp_user = _input(actor_identity=_user_identity(oid="oid-user"))
        inp_svc = _input(actor_identity=_service_identity(app_id="app-svc"))
        assert resolve_field("principal.oid", inp_user) == "oid-user"
        assert resolve_field("principal.oid_or_app_id", inp_user) == "oid-user"
        assert resolve_field("principal.oid_or_app_id", inp_svc) == "app-svc"

    def test_dotted_principal_upn_via_claims(self) -> None:
        inp = _input(actor_identity=_user_identity(upn="carol@x"))
        assert resolve_field("principal.upn", inp) == "carol@x"

    def test_dotted_principal_with_no_identity(self) -> None:
        inp = _input()
        assert resolve_field("principal.type", inp) is None
        assert resolve_field("principal.id", inp) is None
        assert resolve_field("principal.oid_or_app_id", inp) is None


# ---------------------------------------------------------------------------
# End-to-end routing via MipRouter
# ---------------------------------------------------------------------------


class TestPrincipalTypeRouting:
    """Confirm rules can split traffic by principal type and that
    ``actor_identity=None`` disables principal_* rules cleanly."""

    def test_user_token_routes_to_user_rule(self) -> None:
        cfg = MipConfig(rules=[
            _rule("user-rule", 10, bank="user-bank", principal_type="user"),
            _rule("svc-rule", 10, bank="svc-bank", principal_type="service"),
        ])
        cfg.tie_breaker = "error"  # there must be no ambiguity
        decision = MipRouter(cfg).route_sync(_input(actor_identity=_user_identity()))
        assert decision is not None
        assert decision.rule_name == "user-rule"
        assert decision.bank_id == "user-bank"

    def test_service_token_routes_to_service_rule(self) -> None:
        cfg = MipConfig(rules=[
            _rule("user-rule", 10, bank="user-bank", principal_type="user"),
            _rule("svc-rule", 10, bank="svc-bank", principal_type="service"),
        ])
        cfg.tie_breaker = "error"
        decision = MipRouter(cfg).route_sync(_input(actor_identity=_service_identity()))
        assert decision is not None
        assert decision.rule_name == "svc-rule"
        assert decision.bank_id == "svc-bank"

    def test_no_identity_skips_principal_rules(self) -> None:
        """Rules with ``principal_type`` must not fire for anonymous
        callers — otherwise enabling JWT middleware would be a breaking
        change for legacy integrations."""
        cfg = MipConfig(rules=[
            _rule("principal-only", 10, bank="principal-bank", principal_type="user"),
            _rule("generic", 20, bank="generic-bank"),
        ])
        decision = MipRouter(cfg).route_sync(_input())  # no actor_identity
        assert decision is not None
        assert decision.rule_name == "generic"

    def test_specific_principal_id_filter_matches(self) -> None:
        """Rules can pin to a specific principal (e.g. an admin OID)."""
        cfg = MipConfig(rules=[
            _rule(
                "admin-only", 5, bank="admin-bank",
                principal_type="user", principal_id="admin-oid",
            ),
        ])
        admin = _input(actor_identity=_user_identity(oid="admin-oid"))
        decision = MipRouter(cfg).route_sync(admin)
        assert decision is not None
        assert decision.rule_name == "admin-only"

    def test_specific_principal_id_filter_excludes_non_admin(self) -> None:
        """The same rule must NOT match a different OID — otherwise the
        admin filter is not actually filtering."""
        cfg = MipConfig(rules=[
            _rule(
                "admin-only", 5, bank="admin-bank",
                principal_type="user", principal_id="admin-oid",
            ),
        ])
        other = _input(actor_identity=_user_identity(oid="some-other-oid"))
        decision = MipRouter(cfg).route_sync(other)
        # No rule matched → None (no fallback rule here).
        assert decision is None


class TestPrincipalTemplateInterpolation:
    """Bank and tag templates must interpolate identity fields."""

    def test_bank_template_with_principal_oid(self) -> None:
        cfg = MipConfig(rules=[
            RoutingRule(
                name="per-user",
                priority=10,
                match=MatchBlock(all_conditions=[
                    MatchSpec(field="content_type", operator="eq", value="text"),
                    MatchSpec(field="principal_type", operator="eq", value="user"),
                ]),
                action=ActionSpec(bank="user-{principal.oid_or_app_id}"),
            ),
        ])
        decision = MipRouter(cfg).route_sync(
            _input(actor_identity=_user_identity(oid="alice-9")),
        )
        assert decision is not None
        assert decision.bank_id == "user-alice-9"

    def test_bank_template_with_principal_app_id_for_service(self) -> None:
        cfg = MipConfig(rules=[
            RoutingRule(
                name="per-svc",
                priority=10,
                match=MatchBlock(all_conditions=[
                    MatchSpec(field="content_type", operator="eq", value="text"),
                    MatchSpec(field="principal_type", operator="eq", value="service"),
                ]),
                action=ActionSpec(bank="svc-{principal.app_id}"),
            ),
        ])
        decision = MipRouter(cfg).route_sync(
            _input(actor_identity=_service_identity(app_id="svc-77")),
        )
        assert decision is not None
        assert decision.bank_id == "svc-svc-77"  # template prefix + id

    def test_tag_template_with_principal_upn(self) -> None:
        cfg = MipConfig(rules=[
            RoutingRule(
                name="tagged",
                priority=10,
                match=MatchBlock(all_conditions=[
                    MatchSpec(field="content_type", operator="eq", value="text"),
                    MatchSpec(field="principal_type", operator="eq", value="user"),
                ]),
                action=ActionSpec(bank="b", tags=["actor:{principal.upn}"]),
            ),
        ])
        decision = MipRouter(cfg).route_sync(
            _input(actor_identity=_user_identity(upn="dana@co")),
        )
        assert decision is not None
        assert decision.tags == ["actor:dana@co"]


class TestBeneficiaryProxyRouting:
    """Gap 4: service account + ``metadata.beneficiary_user_id`` → user bank.

    This is a pure-MIP test — no SDK changes are required for the
    beneficiary convention to work. The reference rule encodes the
    contract documented in ``docs/_plugins/proxied-user-identity.md``."""

    @staticmethod
    def _proxy_rule() -> RoutingRule:
        return RoutingRule(
            name="proxied-user-data",
            priority=8,
            match=MatchBlock(all_conditions=[
                MatchSpec(field="content_type", operator="eq", value="text"),
                MatchSpec(field="principal_type", operator="eq", value="service"),
                MatchSpec(field="metadata.beneficiary_user_id", operator="present"),
            ]),
            action=ActionSpec(
                bank="user-{metadata.beneficiary_user_id}",
                tags=["proxied", "m2m-on-behalf"],
            ),
        )

    @staticmethod
    def _svc_default_rule_with_beneficiary_absent() -> RoutingRule:
        """Service-default rule that's structurally exclusive with the
        proxy rule — it refuses to match when beneficiary is present
        (via ``none_conditions``). This is the pattern the reference
        enterprise mip.yaml uses to keep the two rules non-overlapping."""
        return RoutingRule(
            name="svc-default",
            priority=100,
            match=MatchBlock(
                all_conditions=[
                    MatchSpec(field="content_type", operator="eq", value="text"),
                    MatchSpec(field="principal_type", operator="eq", value="service"),
                ],
                none_conditions=[
                    MatchSpec(field="metadata.beneficiary_user_id", operator="present"),
                ],
            ),
            action=ActionSpec(bank="svc-{principal.app_id}"),
        )

    def test_proxied_call_routes_to_user_bank(self) -> None:
        cfg = MipConfig(rules=[
            self._proxy_rule(),
            self._svc_default_rule_with_beneficiary_absent(),
        ])
        decision = MipRouter(cfg).route_sync(_input(
            actor_identity=_service_identity(app_id="hr-bot"),
            metadata={"beneficiary_user_id": "user-42"},
        ))
        assert decision is not None
        assert decision.rule_name == "proxied-user-data"
        assert decision.bank_id == "user-user-42"
        assert decision.tags == ["proxied", "m2m-on-behalf"]

    def test_service_call_without_beneficiary_falls_through(self) -> None:
        """Without a beneficiary declaration, data must land in the
        machine bank — Astrocyte never guesses which human a proxied
        call is for."""
        cfg = MipConfig(rules=[
            self._proxy_rule(),
            self._svc_default_rule_with_beneficiary_absent(),
        ])
        decision = MipRouter(cfg).route_sync(_input(
            actor_identity=_service_identity(app_id="hr-bot"),
            # no beneficiary_user_id metadata
        ))
        assert decision is not None
        assert decision.rule_name == "svc-default"
        assert decision.bank_id == "svc-hr-bot"

    def test_user_call_not_matched_by_proxy_rule(self) -> None:
        """A real delegated user token must not be routed through the
        proxy rule even if beneficiary metadata is accidentally present
        — that would double-account the memory."""
        cfg = MipConfig(rules=[self._proxy_rule()])
        decision = MipRouter(cfg).route_sync(_input(
            actor_identity=_user_identity(oid="alice"),
            metadata={"beneficiary_user_id": "carol"},
        ))
        # The proxy rule requires principal_type=service, so no match.
        # Falling out of the rule set here is the correct behavior —
        # the real config would have a user-default rule to catch this.
        assert decision is None
