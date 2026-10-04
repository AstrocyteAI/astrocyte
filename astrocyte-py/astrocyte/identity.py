"""Identity resolution, OBO permission intersection, and bank resolution (M1 / v0.5.0 with M2).

See ``docs/_design/adr/adr-002-identity-model.md``.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from astrocyte.types import AccessGrant, ActorIdentity, AstrocyteContext


def format_principal(actor: ActorIdentity) -> str:
    """Serialize actor to the grant-matching principal form ``{type}:{id}``."""
    return f"{actor.type}:{actor.id}"


def parse_principal(principal: str) -> ActorIdentity:
    """Parse ``principal`` string into :class:`ActorIdentity` (ADR-002).

    - ``agent:X`` / ``user:X`` / ``service:X`` / ``team:X`` → typed id ``X``
    - no prefix → treated as ``user:{principal}``
    """
    for prefix, typ in (("agent:", "agent"), ("user:", "user"), ("service:", "service"), ("team:", "team")):
        if principal.startswith(prefix):
            return ActorIdentity(type=typ, id=principal[len(prefix) :])
    return ActorIdentity(type="user", id=principal)


def resolve_actor(context: AstrocyteContext) -> ActorIdentity | None:
    """Resolved actor: explicit ``context.actor``, else parse ``context.principal``."""
    if context.actor is not None:
        return context.actor
    if context.principal:
        return parse_principal(context.principal)
    return None


def context_principal_label(context: AstrocyteContext) -> str:
    """Short label for logs and :class:`~astrocyte.errors.AccessDenied` messages."""
    actor = resolve_actor(context)
    base = format_principal(actor) if actor else "anonymous"
    if context.on_behalf_of is not None:
        return f"{base} obo {format_principal(context.on_behalf_of)}"
    return base


_GLOB_CHARS = "*?["


def _is_pattern(value: str) -> bool:
    return any(c in value for c in _GLOB_CHARS)


def _grant_field_matches(pattern: str, value: str) -> bool:
    """Match a grant's ``bank_id`` or ``principal`` against a concrete value.

    ``*`` and exact strings behave as they always have. Any other value with a
    glob character is an fnmatch pattern (``project:*``, ``user:*``), matched
    case-sensitively on every platform. Bank ids cannot contain glob characters
    (``validate_bank_id``), so a pattern never collides with a real id.
    """
    if pattern == "*" or pattern == value:
        return True
    return _is_pattern(pattern) and fnmatchcase(value, pattern)


def _permissions_for_principal_on_bank(
    grants: list[AccessGrant],
    bank_id: str,
    principal_str: str,
    groups: list[str] | None = None,
) -> set[str]:
    principals = [principal_str, *(groups or ())]
    out: set[str] = set()
    for g in grants:
        if not _grant_field_matches(g.bank_id, bank_id):
            continue
        if any(_grant_field_matches(g.principal, p) for p in principals):
            out.update(g.permissions)
    return out


def _with_context_grants(context: AstrocyteContext, grants: list[AccessGrant]) -> list[AccessGrant]:
    return [*grants, *context.grants] if context.grants else grants


def effective_permissions(
    context: AstrocyteContext,
    grants: list[AccessGrant],
    bank_id: str,
) -> set[str]:
    """Effective permission set for ``bank_id``, including OBO intersection.

    Grants carried on the context (``context.grants``, e.g. bound to a gateway
    token) are added to ``grants``. ``context.groups`` are extra principals the
    actor holds, so a grant to ``team:api`` applies to its members; they do not
    extend the on-behalf-of identity.
    """
    actor = resolve_actor(context)
    if actor is None:
        return set()
    all_grants = _with_context_grants(context, grants)
    actor_perms = _permissions_for_principal_on_bank(
        all_grants, bank_id, format_principal(actor), groups=context.groups
    )
    if context.on_behalf_of is None:
        return actor_perms
    obo_perms = _permissions_for_principal_on_bank(all_grants, bank_id, format_principal(context.on_behalf_of))
    return actor_perms & obo_perms


def accessible_read_banks(
    context: AstrocyteContext,
    grants: list[AccessGrant],
    *,
    known_bank_ids: list[str] | None = None,
    resolver: BankResolver | None = None,
) -> list[str]:
    """Bank IDs where ``context`` has effective ``read`` after OBO intersection.

    Candidates are: non-pattern ``bank_id`` values from ``grants`` and
    ``context.grants``, optional ``known_bank_ids``, and (when no other
    candidates) a convention bank from ``resolver`` for the resolved actor so
    wildcard-only ACL rows can still resolve.
    """
    candidates: set[str] = {g.bank_id for g in _with_context_grants(context, grants) if not _is_pattern(g.bank_id)}
    if known_bank_ids:
        candidates.update(known_bank_ids)

    actor = resolve_actor(context)
    if not candidates and resolver is not None and actor is not None:
        suggested = resolver.default_bank_id(actor)
        if suggested is not None:
            candidates.add(suggested)

    out: list[str] = []
    for bid in sorted(candidates):
        if "read" in effective_permissions(context, grants, bid):
            out.append(bid)
    return out


class BankResolver:
    """Convention-based default bank id from a resolved :class:`ActorIdentity`."""

    def __init__(
        self,
        *,
        user_prefix: str = "user-",
        agent_prefix: str = "agent-",
        service_prefix: str = "service-",
    ) -> None:
        self._user_prefix = user_prefix
        self._agent_prefix = agent_prefix
        self._service_prefix = service_prefix

    def default_bank_id(self, actor: ActorIdentity) -> str | None:
        if actor.type == "user":
            return f"{self._user_prefix}{actor.id}"
        if actor.type == "agent":
            return f"{self._agent_prefix}{actor.id}"
        if actor.type == "service":
            return f"{self._service_prefix}{actor.id}"
        return None
