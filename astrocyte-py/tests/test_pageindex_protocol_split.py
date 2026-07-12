"""Pins the PageIndexStore ISP split (role protocols + composed union).

If a future edit drops a role from the union bases, or moves a method out of a
role without re-homing it, these fail loudly rather than silently shrinking the
SPI that implementers are checked against.
"""

from __future__ import annotations

from astrocyte.provider import (
    PageIndexDocumentStore,
    PageIndexFactStore,
    PageIndexLifecycle,
    PageIndexSectionSearch,
    PageIndexSectionWriter,
    PageIndexStore,
    PageIndexWikiStore,
)
from astrocyte.testing.in_memory import InMemoryPageIndexStore

_ROLES = (
    PageIndexDocumentStore,
    PageIndexSectionWriter,
    PageIndexSectionSearch,
    PageIndexFactStore,
    PageIndexWikiStore,
    PageIndexLifecycle,
)


def _public_methods(cls: type) -> set[str]:
    return {n for n in dir(cls) if not n.startswith("_")}


def test_union_inherits_every_role() -> None:
    # Protocols aren't @runtime_checkable, so assert via the MRO (the union
    # literally inherits each role).
    mro = PageIndexStore.__mro__
    for role in _ROLES:
        assert role in mro, f"PageIndexStore lost role {role.__name__}"


def test_union_surface_is_exactly_the_role_union() -> None:
    from_roles: set[str] = set()
    for role in _ROLES:
        from_roles |= _public_methods(role)
    assert _public_methods(PageIndexStore) == from_roles


def test_reference_store_satisfies_every_role_method() -> None:
    store = InMemoryPageIndexStore()
    missing = {m for m in _public_methods(PageIndexStore) if not hasattr(store, m)}
    assert not missing, f"InMemoryPageIndexStore is missing {sorted(missing)}"


def test_roles_are_disjoint() -> None:
    """No method should live in two role protocols (a clean partition)."""
    seen: dict[str, str] = {}
    for role in _ROLES:
        for m in _public_methods(role):
            if m == "SPI_VERSION":  # ClassVar, allowed to surface broadly
                continue
            assert m not in seen or seen[m] == role.__name__, (
                f"{m} appears in both {seen.get(m)} and {role.__name__}"
            )
            seen[m] = role.__name__
